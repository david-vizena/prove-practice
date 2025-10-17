"""
Product Service - E-commerce Product Catalog API
Demonstrates OpenTelemetry instrumentation, metrics, and distributed tracing
"""

import os
import logging
import structlog
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from opentelemetry import trace
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.metrics import get_meter
import time
import random

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///products.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize database
db = SQLAlchemy(app)

# Product model
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Float, nullable=False)
    stock_quantity = db.Column(db.Integer, default=0)
    category = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=db.func.current_timestamp())
    
    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'price': self.price,
            'stock_quantity': self.stock_quantity,
            'category': self.category,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

# OpenTelemetry setup
def setup_telemetry():
    """Configure OpenTelemetry tracing and metrics"""
    
    # Create resource with service information
    resource = Resource.create({
        ResourceAttributes.SERVICE_NAME: "product-service",
        ResourceAttributes.SERVICE_VERSION: "1.0.0",
        ResourceAttributes.DEPLOYMENT_ENVIRONMENT: os.getenv("ENVIRONMENT", "development")
    })
    
    # Setup tracing
    trace.set_tracer_provider(TracerProvider(resource=resource))
    tracer = trace.get_tracer(__name__)
    
    # Jaeger exporter for distributed tracing
    jaeger_exporter = JaegerExporter(
        agent_host_name=os.getenv("JAEGER_AGENT_HOST", "localhost"),
        agent_port=int(os.getenv("JAEGER_AGENT_PORT", "14268")),
    )
    
    # Batch span processor for efficient export
    span_processor = BatchSpanProcessor(jaeger_exporter)
    trace.get_tracer_provider().add_span_processor(span_processor)
    
    # Setup metrics
    prometheus_reader = PrometheusMetricReader()
    meter_provider = MeterProvider(resource=resource, metric_readers=[prometheus_reader])
    
    # Instrument Flask and SQLAlchemy
    FlaskInstrumentor().instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=db.engine)
    RequestsInstrumentor().instrument()
    
    return tracer, get_meter(__name__)

# Initialize telemetry (will be called after app is created)
tracer = None
meter = None

# Custom metrics (will be initialized after telemetry setup)
request_counter = None
request_duration = None
products_in_stock = None

# Health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Kubernetes liveness/readiness probes"""
    try:
        # Check database connectivity
        db.session.execute('SELECT 1')
        return jsonify({
            'status': 'healthy',
            'service': 'product-service',
            'version': '1.0.0',
            'database': 'connected'
        }), 200
    except Exception as e:
        logger.error("Health check failed", error=str(e))
        return jsonify({
            'status': 'unhealthy',
            'service': 'product-service',
            'error': str(e)
        }), 503

# Get all products
@app.route('/products', methods=['GET'])
def get_products():
    """Get all products with optional filtering"""
    with tracer.start_as_current_span("get_products") as span:
        start_time = time.time()
        
        try:
            # Add span attributes
            span.set_attribute("http.method", "GET")
            span.set_attribute("http.url", request.url)
            span.set_attribute("service.name", "product-service")
            
            # Parse query parameters
            category = request.args.get('category')
            limit = request.args.get('limit', type=int)
            offset = request.args.get('offset', 0, type=int)
            
            # Build query
            query = Product.query
            if category:
                query = query.filter(Product.category == category)
                span.set_attribute("filter.category", category)
            
            if limit:
                query = query.limit(limit)
                span.set_attribute("filter.limit", limit)
            
            query = query.offset(offset)
            
            # Execute query
            products = query.all()
            
            # Record metrics
            request_counter.add(1, {"method": "GET", "endpoint": "/products"})
            request_duration.record(time.time() - start_time, {"method": "GET", "endpoint": "/products"})
            
            # Log with trace context
            logger.info("Retrieved products", 
                      count=len(products), 
                      category=category,
                      limit=limit,
                      offset=offset)
            
            return jsonify({
                'products': [product.to_dict() for product in products],
                'total': len(products),
                'category': category,
                'limit': limit,
                'offset': offset
            }), 200
            
        except Exception as e:
            span.record_exception(e)
            logger.error("Failed to retrieve products", error=str(e))
            return jsonify({'error': 'Failed to retrieve products'}), 500

# Get product by ID
@app.route('/products/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """Get a specific product by ID"""
    with tracer.start_as_current_span("get_product") as span:
        start_time = time.time()
        
        try:
            span.set_attribute("product.id", product_id)
            span.set_attribute("http.method", "GET")
            span.set_attribute("http.url", request.url)
            
            product = Product.query.get_or_404(product_id)
            
            # Record metrics
            request_counter.add(1, {"method": "GET", "endpoint": "/products/{id}"})
            request_duration.record(time.time() - start_time, {"method": "GET", "endpoint": "/products/{id}"})
            
            logger.info("Retrieved product", product_id=product_id, product_name=product.name)
            
            return jsonify(product.to_dict()), 200
            
        except Exception as e:
            span.record_exception(e)
            logger.error("Failed to retrieve product", product_id=product_id, error=str(e))
            return jsonify({'error': 'Product not found'}), 404

# Create new product
@app.route('/products', methods=['POST'])
def create_product():
    """Create a new product"""
    with tracer.start_as_current_span("create_product") as span:
        start_time = time.time()
        
        try:
            data = request.get_json()
            
            # Validate required fields
            required_fields = ['name', 'price']
            for field in required_fields:
                if field not in data:
                    return jsonify({'error': f'Missing required field: {field}'}), 400
            
            # Create product
            product = Product(
                name=data['name'],
                description=data.get('description', ''),
                price=data['price'],
                stock_quantity=data.get('stock_quantity', 0),
                category=data.get('category', 'uncategorized')
            )
            
            db.session.add(product)
            db.session.commit()
            
            # Update metrics
            products_in_stock.add(product.stock_quantity)
            
            # Record metrics
            request_counter.add(1, {"method": "POST", "endpoint": "/products"})
            request_duration.record(time.time() - start_time, {"method": "POST", "endpoint": "/products"})
            
            logger.info("Created product", 
                       product_id=product.id, 
                       product_name=product.name,
                       price=product.price,
                       stock=product.stock_quantity)
            
            return jsonify(product.to_dict()), 201
            
        except Exception as e:
            db.session.rollback()
            span.record_exception(e)
            logger.error("Failed to create product", error=str(e))
            return jsonify({'error': 'Failed to create product'}), 500

# Update product stock
@app.route('/products/<int:product_id>/stock', methods=['PUT'])
def update_stock(product_id):
    """Update product stock quantity"""
    with tracer.start_as_current_span("update_stock") as span:
        start_time = time.time()
        
        try:
            data = request.get_json()
            new_quantity = data.get('quantity')
            
            if new_quantity is None:
                return jsonify({'error': 'Missing quantity field'}), 400
            
            product = Product.query.get_or_404(product_id)
            old_quantity = product.stock_quantity
            product.stock_quantity = new_quantity
            db.session.commit()
            
            # Update metrics
            products_in_stock.add(new_quantity - old_quantity)
            
            # Record metrics
            request_counter.add(1, {"method": "PUT", "endpoint": "/products/{id}/stock"})
            request_duration.record(time.time() - start_time, {"method": "PUT", "endpoint": "/products/{id}/stock"})
            
            logger.info("Updated product stock", 
                       product_id=product_id,
                       old_quantity=old_quantity,
                       new_quantity=new_quantity)
            
            return jsonify(product.to_dict()), 200
            
        except Exception as e:
            db.session.rollback()
            span.record_exception(e)
            logger.error("Failed to update stock", product_id=product_id, error=str(e))
            return jsonify({'error': 'Failed to update stock'}), 500

# Simulate some latency for testing
@app.route('/products/slow', methods=['GET'])
def slow_products():
    """Simulate slow response for testing observability"""
    with tracer.start_as_current_span("slow_products") as span:
        # Simulate processing time
        delay = random.uniform(1, 3)
        time.sleep(delay)
        
        span.set_attribute("simulated.delay", delay)
        logger.warning("Slow endpoint accessed", delay=delay)
        
        return jsonify({
            'message': 'This is a slow endpoint for testing',
            'delay': delay,
            'products': []
        }), 200

# Initialize database function
def create_tables():
    """Create database tables and seed with sample data"""
    db.create_all()
    
    # Seed with sample data if empty
    if Product.query.count() == 0:
        sample_products = [
            Product(name="Laptop Pro", description="High-performance laptop", price=1299.99, stock_quantity=50, category="electronics"),
            Product(name="Wireless Mouse", description="Ergonomic wireless mouse", price=29.99, stock_quantity=200, category="electronics"),
            Product(name="Coffee Maker", description="Automatic coffee maker", price=89.99, stock_quantity=30, category="appliances"),
            Product(name="Running Shoes", description="Comfortable running shoes", price=79.99, stock_quantity=100, category="clothing"),
            Product(name="Book: SRE Guide", description="Site Reliability Engineering book", price=24.99, stock_quantity=75, category="books"),
        ]
        
        for product in sample_products:
            db.session.add(product)
        
        db.session.commit()
        logger.info("Seeded database with sample products", count=len(sample_products))

if __name__ == '__main__':
    # Initialize telemetry after app is created
    tracer, meter = setup_telemetry()
    
    # Initialize metrics
    request_counter = meter.create_counter(
        name="product_service_requests_total",
        description="Total number of requests to product service",
        unit="1"
    )
    
    request_duration = meter.create_histogram(
        name="product_service_request_duration_seconds",
        description="Request duration in seconds",
        unit="s"
    )
    
    products_in_stock = meter.create_up_down_counter(
        name="products_in_stock_total",
        description="Total number of products in stock",
        unit="1"
    )
    
    # Create tables and seed data
    with app.app_context():
        create_tables()
    
    # Run the application
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('DEBUG', 'False').lower() == 'true'
    
    logger.info("Starting Product Service", port=port, debug=debug)
    app.run(host='0.0.0.0', port=port, debug=debug)
