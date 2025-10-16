package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/lib/pq"
	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/contrib/instrumentation/github.com/gin-gonic/gin/otelgin"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/jaeger"
	"go.opentelemetry.io/otel/exporters/prometheus"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	"go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.21.0"
	"go.opentelemetry.io/otel/trace"
)

// Order represents an order in the system
type Order struct {
	ID          string    `json:"id" db:"id"`
	UserID      string    `json:"user_id" db:"user_id"`
	ProductID   int       `json:"product_id" db:"product_id"`
	Quantity    int       `json:"quantity" db:"quantity"`
	TotalPrice  float64   `json:"total_price" db:"total_price"`
	Status      string    `json:"status" db:"status"`
	CreatedAt   time.Time `json:"created_at" db:"created_at"`
	UpdatedAt   time.Time `json:"updated_at" db:"updated_at"`
}

// OrderRequest represents the request to create an order
type OrderRequest struct {
	UserID    string `json:"user_id" binding:"required"`
	ProductID int    `json:"product_id" binding:"required"`
	Quantity  int    `json:"quantity" binding:"required,min=1"`
}

// Product represents a product from the product service
type Product struct {
	ID           int     `json:"id"`
	Name         string  `json:"name"`
	Price        float64 `json:"price"`
	StockQuantity int    `json:"stock_quantity"`
}

var (
	db        *sql.DB
	tracer    trace.Tracer
	meter     metric.Meter
	requestCounter metric.Int64Counter
	requestDuration metric.Float64Histogram
	ordersTotal metric.Int64Counter
)

// initTelemetry initializes OpenTelemetry tracing and metrics
func initTelemetry() {
	// Create resource
	res, err := resource.New(context.Background(),
		resource.WithAttributes(
			semconv.ServiceName("order-service"),
			semconv.ServiceVersion("1.0.0"),
			semconv.DeploymentEnvironment(os.Getenv("ENVIRONMENT")),
		),
	)
	if err != nil {
		log.Fatalf("failed to create resource: %v", err)
	}

	// Setup tracing
	tp := trace.NewTracerProvider(
		trace.WithBatcher(jaeger.New(jaeger.WithCollectorEndpoint(jaeger.WithEndpoint("http://localhost:14268/api/traces")))),
		trace.WithResource(res),
	)
	otel.SetTracerProvider(tp)
	tracer = otel.Tracer("order-service")

	// Setup metrics
	exporter, err := prometheus.New()
	if err != nil {
		log.Fatalf("failed to create prometheus exporter: %v", err)
	}

	mp := metric.NewMeterProvider(
		metric.WithReader(exporter),
		metric.WithResource(res),
	)
	otel.SetMeterProvider(mp)
	meter = otel.Meter("order-service")

	// Create custom metrics
	requestCounter, _ = meter.Int64Counter("order_service_requests_total", metric.WithDescription("Total number of requests to order service"))
	requestDuration, _ = meter.Float64Histogram("order_service_request_duration_seconds", metric.WithDescription("Request duration in seconds"))
	ordersTotal, _ = meter.Int64Counter("orders_total", metric.WithDescription("Total number of orders created"))
}

// initDatabase initializes the database connection
func initDatabase() {
	var err error
	databaseURL := os.Getenv("DATABASE_URL")
	if databaseURL == "" {
		databaseURL = "postgres://user:password@localhost/orders?sslmode=disable"
	}

	db, err = sql.Open("postgres", databaseURL)
	if err != nil {
		log.Fatalf("failed to connect to database: %v", err)
	}

	// Create orders table
	createTableSQL := `
	CREATE TABLE IF NOT EXISTS orders (
		id VARCHAR(36) PRIMARY KEY,
		user_id VARCHAR(255) NOT NULL,
		product_id INTEGER NOT NULL,
		quantity INTEGER NOT NULL,
		total_price DECIMAL(10,2) NOT NULL,
		status VARCHAR(50) NOT NULL DEFAULT 'pending',
		created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
		updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
	);`

	if _, err := db.Exec(createTableSQL); err != nil {
		log.Fatalf("failed to create table: %v", err)
	}
}

// getProductFromService fetches product details from the product service
func getProductFromService(ctx context.Context, productID int) (*Product, error) {
	span := trace.SpanFromContext(ctx)
	span.SetAttributes(attribute.Int("product.id", productID))

	// Create HTTP client with OpenTelemetry instrumentation
	client := &http.Client{
		Transport: otelhttp.NewTransport(http.DefaultTransport),
	}

	productServiceURL := os.Getenv("PRODUCT_SERVICE_URL")
	if productServiceURL == "" {
		productServiceURL = "http://localhost:5000"
	}

	url := fmt.Sprintf("%s/products/%d", productServiceURL, productID)
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		span.RecordError(err)
		return nil, err
	}

	resp, err := client.Do(req)
	if err != nil {
		span.RecordError(err)
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		span.SetAttributes(attribute.Int("http.status_code", resp.StatusCode))
		return nil, fmt.Errorf("product service returned status %d", resp.StatusCode)
	}

	var product Product
	if err := json.NewDecoder(resp.Body).Decode(&product); err != nil {
		span.RecordError(err)
		return nil, err
	}

	span.SetAttributes(
		attribute.String("product.name", product.Name),
		attribute.Float64("product.price", product.Price),
		attribute.Int("product.stock", product.StockQuantity),
	)

	return &product, nil
}

// createOrder handles POST /orders
func createOrder(c *gin.Context) {
	ctx := c.Request.Context()
	span := trace.SpanFromContext(ctx)
	start := time.Now()

	var req OrderRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		span.RecordError(err)
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	span.SetAttributes(
		attribute.String("user.id", req.UserID),
		attribute.Int("product.id", req.ProductID),
		attribute.Int("order.quantity", req.Quantity),
	)

	// Get product details from product service
	product, err := getProductFromService(ctx, req.ProductID)
	if err != nil {
		span.RecordError(err)
		logrus.WithError(err).Error("Failed to get product from service")
		c.JSON(http.StatusNotFound, gin.H{"error": "Product not found"})
		return
	}

	// Check stock availability
	if product.StockQuantity < req.Quantity {
		span.SetAttributes(attribute.String("error.type", "insufficient_stock"))
		c.JSON(http.StatusBadRequest, gin.H{
			"error": "Insufficient stock",
			"available": product.StockQuantity,
			"requested": req.Quantity,
		})
		return
	}

	// Calculate total price
	totalPrice := product.Price * float64(req.Quantity)

	// Create order
	orderID := uuid.New().String()
	order := Order{
		ID:         orderID,
		UserID:     req.UserID,
		ProductID:  req.ProductID,
		Quantity:   req.Quantity,
		TotalPrice: totalPrice,
		Status:     "pending",
		CreatedAt:  time.Now(),
		UpdatedAt:  time.Now(),
	}

	// Insert into database
	insertSQL := `
		INSERT INTO orders (id, user_id, product_id, quantity, total_price, status, created_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`

	_, err = db.ExecContext(ctx, insertSQL,
		order.ID, order.UserID, order.ProductID, order.Quantity,
		order.TotalPrice, order.Status, order.CreatedAt, order.UpdatedAt)

	if err != nil {
		span.RecordError(err)
		logrus.WithError(err).Error("Failed to create order")
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to create order"})
		return
	}

	// Record metrics
	requestCounter.Add(ctx, 1, metric.WithAttributes(
		attribute.String("method", "POST"),
		attribute.String("endpoint", "/orders"),
	))
	requestDuration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(
		attribute.String("method", "POST"),
		attribute.String("endpoint", "/orders"),
	))
	ordersTotal.Add(ctx, 1)

	span.SetAttributes(
		attribute.String("order.id", orderID),
		attribute.Float64("order.total_price", totalPrice),
		attribute.String("order.status", "pending"),
	)

	logrus.WithFields(logrus.Fields{
		"order_id": orderID,
		"user_id": req.UserID,
		"product_id": req.ProductID,
		"quantity": req.Quantity,
		"total_price": totalPrice,
	}).Info("Order created successfully")

	c.JSON(http.StatusCreated, order)
}

// getOrders handles GET /orders
func getOrders(c *gin.Context) {
	ctx := c.Request.Context()
	span := trace.SpanFromContext(ctx)
	start := time.Now()

	userID := c.Query("user_id")
	status := c.Query("status")
	limitStr := c.DefaultQuery("limit", "10")
	offsetStr := c.DefaultQuery("offset", "0")

	limit, err := strconv.Atoi(limitStr)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid limit parameter"})
		return
	}

	offset, err := strconv.Atoi(offsetStr)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid offset parameter"})
		return
	}

	span.SetAttributes(
		attribute.String("filter.user_id", userID),
		attribute.String("filter.status", status),
		attribute.Int("filter.limit", limit),
		attribute.Int("filter.offset", offset),
	)

	// Build query
	query := "SELECT id, user_id, product_id, quantity, total_price, status, created_at, updated_at FROM orders WHERE 1=1"
	args := []interface{}{}
	argIndex := 1

	if userID != "" {
		query += fmt.Sprintf(" AND user_id = $%d", argIndex)
		args = append(args, userID)
		argIndex++
	}

	if status != "" {
		query += fmt.Sprintf(" AND status = $%d", argIndex)
		args = append(args, status)
		argIndex++
	}

	query += fmt.Sprintf(" ORDER BY created_at DESC LIMIT $%d OFFSET $%d", argIndex, argIndex+1)
	args = append(args, limit, offset)

	rows, err := db.QueryContext(ctx, query, args...)
	if err != nil {
		span.RecordError(err)
		logrus.WithError(err).Error("Failed to query orders")
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to retrieve orders"})
		return
	}
	defer rows.Close()

	var orders []Order
	for rows.Next() {
		var order Order
		err := rows.Scan(&order.ID, &order.UserID, &order.ProductID, &order.Quantity,
			&order.TotalPrice, &order.Status, &order.CreatedAt, &order.UpdatedAt)
		if err != nil {
			span.RecordError(err)
			logrus.WithError(err).Error("Failed to scan order")
			continue
		}
		orders = append(orders, order)
	}

	// Record metrics
	requestCounter.Add(ctx, 1, metric.WithAttributes(
		attribute.String("method", "GET"),
		attribute.String("endpoint", "/orders"),
	))
	requestDuration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(
		attribute.String("method", "GET"),
		attribute.String("endpoint", "/orders"),
	))

	span.SetAttributes(attribute.Int("orders.count", len(orders)))

	logrus.WithFields(logrus.Fields{
		"user_id": userID,
		"status": status,
		"count": len(orders),
	}).Info("Retrieved orders")

	c.JSON(http.StatusOK, gin.H{
		"orders": orders,
		"total": len(orders),
		"limit": limit,
		"offset": offset,
	})
}

// getOrder handles GET /orders/:id
func getOrder(c *gin.Context) {
	ctx := c.Request.Context()
	span := trace.SpanFromContext(ctx)
	start := time.Now()

	orderID := c.Param("id")
	span.SetAttributes(attribute.String("order.id", orderID))

	query := "SELECT id, user_id, product_id, quantity, total_price, status, created_at, updated_at FROM orders WHERE id = $1"
	row := db.QueryRowContext(ctx, query, orderID)

	var order Order
	err := row.Scan(&order.ID, &order.UserID, &order.ProductID, &order.Quantity,
		&order.TotalPrice, &order.Status, &order.CreatedAt, &order.UpdatedAt)

	if err != nil {
		if err == sql.ErrNoRows {
			span.SetAttributes(attribute.String("error.type", "not_found"))
			c.JSON(http.StatusNotFound, gin.H{"error": "Order not found"})
			return
		}
		span.RecordError(err)
		logrus.WithError(err).Error("Failed to retrieve order")
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to retrieve order"})
		return
	}

	// Record metrics
	requestCounter.Add(ctx, 1, metric.WithAttributes(
		attribute.String("method", "GET"),
		attribute.String("endpoint", "/orders/{id}"),
	))
	requestDuration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(
		attribute.String("method", "GET"),
		attribute.String("endpoint", "/orders/{id}"),
	))

	logrus.WithField("order_id", orderID).Info("Retrieved order")
	c.JSON(http.StatusOK, order)
}

// updateOrderStatus handles PUT /orders/:id/status
func updateOrderStatus(c *gin.Context) {
	ctx := c.Request.Context()
	span := trace.SpanFromContext(ctx)
	start := time.Now()

	orderID := c.Param("id")
	span.SetAttributes(attribute.String("order.id", orderID))

	var req struct {
		Status string `json:"status" binding:"required"`
	}

	if err := c.ShouldBindJSON(&req); err != nil {
		span.RecordError(err)
		c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
		return
	}

	span.SetAttributes(attribute.String("order.status", req.Status))

	query := "UPDATE orders SET status = $1, updated_at = $2 WHERE id = $3"
	result, err := db.ExecContext(ctx, query, req.Status, time.Now(), orderID)
	if err != nil {
		span.RecordError(err)
		logrus.WithError(err).Error("Failed to update order status")
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to update order status"})
		return
	}

	rowsAffected, err := result.RowsAffected()
	if err != nil {
		span.RecordError(err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to update order status"})
		return
	}

	if rowsAffected == 0 {
		span.SetAttributes(attribute.String("error.type", "not_found"))
		c.JSON(http.StatusNotFound, gin.H{"error": "Order not found"})
		return
	}

	// Record metrics
	requestCounter.Add(ctx, 1, metric.WithAttributes(
		attribute.String("method", "PUT"),
		attribute.String("endpoint", "/orders/{id}/status"),
	))
	requestDuration.Record(ctx, time.Since(start).Seconds(), metric.WithAttributes(
		attribute.String("method", "PUT"),
		attribute.String("endpoint", "/orders/{id}/status"),
	))

	logrus.WithFields(logrus.Fields{
		"order_id": orderID,
		"status": req.Status,
	}).Info("Updated order status")

	c.JSON(http.StatusOK, gin.H{"message": "Order status updated successfully"})
}

// healthCheck handles GET /health
func healthCheck(c *gin.Context) {
	ctx := c.Request.Context()
	
	// Check database connectivity
	if err := db.PingContext(ctx); err != nil {
		logrus.WithError(err).Error("Health check failed - database not accessible")
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"status": "unhealthy",
			"service": "order-service",
			"error": err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"status": "healthy",
		"service": "order-service",
		"version": "1.0.0",
		"database": "connected",
	})
}

func main() {
	// Initialize components
	initTelemetry()
	initDatabase()

	// Setup Gin router with OpenTelemetry instrumentation
	r := gin.Default()
	r.Use(otelgin.Middleware("order-service"))

	// Routes
	r.GET("/health", healthCheck)
	r.POST("/orders", createOrder)
	r.GET("/orders", getOrders)
	r.GET("/orders/:id", getOrder)
	r.PUT("/orders/:id/status", updateOrderStatus)

	// Start server
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	logrus.WithField("port", port).Info("Starting Order Service")
	log.Fatal(r.Run(":" + port))
}
