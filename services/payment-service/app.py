"""
Payment Service - Simulates payment processing with failure scenarios
Demonstrates OpenTelemetry instrumentation, error handling, and incident simulation
"""

import asyncio
import os
import random
import time
import uuid
from datetime import datetime
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.metrics import get_meter
import httpx
from pydantic import BaseModel

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

# Pydantic models
class PaymentRequest(BaseModel):
    order_id: str
    user_id: str
    amount: float
    payment_method: str
    card_number: Optional[str] = None
    cvv: Optional[str] = None
    expiry_date: Optional[str] = None

class PaymentResponse(BaseModel):
    payment_id: str
    order_id: str
    status: str
    amount: float
    processed_at: datetime
    failure_reason: Optional[str] = None

class PaymentStatus(BaseModel):
    payment_id: str
    status: str
    amount: float
    processed_at: datetime
    failure_reason: Optional[str] = None

# Global variables for failure simulation
FAILURE_RATE = float(os.getenv("PAYMENT_FAILURE_RATE", "0.1"))  # 10% failure rate by default
LATENCY_SIMULATION = os.getenv("PAYMENT_LATENCY", "normal")  # normal, high, extreme
SERVICE_DOWN = os.getenv("PAYMENT_SERVICE_DOWN", "false").lower() == "true"

# Initialize FastAPI app
app = FastAPI(
    title="Payment Service",
    description="Payment processing service with failure simulation for SRE training",
    version="1.0.0"
)

# OpenTelemetry setup
def setup_telemetry():
    """Configure OpenTelemetry tracing and metrics"""
    
    # Create resource with service information
    resource = Resource.create({
        ResourceAttributes.SERVICE_NAME: "payment-service",
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
    
    # Instrument FastAPI and HTTP client
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    
    return tracer, get_meter(__name__)

# Initialize telemetry
tracer, meter = setup_telemetry()

# Custom metrics
payment_requests = meter.create_counter(
    name="payment_requests_total",
    description="Total number of payment requests",
    unit="1"
)

payment_duration = meter.create_histogram(
    name="payment_processing_duration_seconds",
    description="Payment processing duration in seconds",
    unit="s"
)

payment_failures = meter.create_counter(
    name="payment_failures_total",
    description="Total number of payment failures",
    unit="1"
)

payment_amount = meter.create_histogram(
    name="payment_amount_dollars",
    description="Payment amounts in dollars",
    unit="USD"
)

# In-memory storage for demo (in production, use a database)
payments_db = {}

# Simulate payment processing with various failure scenarios
async def simulate_payment_processing(payment_request: PaymentRequest) -> PaymentResponse:
    """Simulate payment processing with configurable failure scenarios"""
    
    with tracer.start_as_current_span("process_payment") as span:
        start_time = time.time()
        
        # Add span attributes
        span.set_attribute("payment.order_id", payment_request.order_id)
        span.set_attribute("payment.user_id", payment_request.user_id)
        span.set_attribute("payment.amount", payment_request.amount)
        span.set_attribute("payment.method", payment_request.payment_method)
        
        # Simulate different latency scenarios
        if LATENCY_SIMULATION == "high":
            delay = random.uniform(2, 5)
        elif LATENCY_SIMULATION == "extreme":
            delay = random.uniform(5, 10)
        else:  # normal
            delay = random.uniform(0.5, 2)
        
        span.set_attribute("simulation.delay", delay)
        await asyncio.sleep(delay)
        
        # Simulate service down scenario
        if SERVICE_DOWN:
            span.set_attribute("error.type", "service_down")
            raise HTTPException(status_code=503, detail="Payment service is temporarily unavailable")
        
        # Simulate random failures
        if random.random() < FAILURE_RATE:
            failure_reasons = [
                "Insufficient funds",
                "Card declined",
                "Invalid card number",
                "Expired card",
                "Network timeout",
                "Bank processing error"
            ]
            failure_reason = random.choice(failure_reasons)
            
            span.set_attribute("error.type", "payment_failed")
            span.set_attribute("error.reason", failure_reason)
            
            payment_failures.add(1, {"reason": failure_reason})
            
            logger.warning("Payment failed", 
                         order_id=payment_request.order_id,
                         amount=payment_request.amount,
                         reason=failure_reason)
            
            return PaymentResponse(
                payment_id=str(uuid.uuid4()),
                order_id=payment_request.order_id,
                status="failed",
                amount=payment_request.amount,
                processed_at=datetime.now(),
                failure_reason=failure_reason
            )
        
        # Simulate successful payment
        payment_id = str(uuid.uuid4())
        processed_at = datetime.now()
        
        # Record metrics
        payment_requests.add(1, {"status": "success"})
        payment_duration.record(time.time() - start_time, {"status": "success"})
        payment_amount.record(payment_request.amount, {"status": "success"})
        
        span.set_attribute("payment.id", payment_id)
        span.set_attribute("payment.status", "success")
        
        logger.info("Payment processed successfully",
                   payment_id=payment_id,
                   order_id=payment_request.order_id,
                   amount=payment_request.amount)
        
        return PaymentResponse(
            payment_id=payment_id,
            order_id=payment_request.order_id,
            status="success",
            amount=payment_request.amount,
            processed_at=processed_at
        )

# API Endpoints

@app.get("/health")
async def health_check():
    """Health check endpoint for Kubernetes probes"""
    if SERVICE_DOWN:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "payment-service",
                "error": "Service is down for simulation"
            }
        )
    
    return {
        "status": "healthy",
        "service": "payment-service",
        "version": "1.0.0",
        "failure_rate": FAILURE_RATE,
        "latency_simulation": LATENCY_SIMULATION
    }

@app.post("/payments", response_model=PaymentResponse)
async def create_payment(payment_request: PaymentRequest):
    """Process a payment request"""
    with tracer.start_as_current_span("create_payment") as span:
        start_time = time.time()
        
        try:
            # Validate payment request
            if payment_request.amount <= 0:
                raise HTTPException(status_code=400, detail="Amount must be positive")
            
            if payment_request.amount > 10000:  # Simulate high-value transaction limits
                span.set_attribute("error.type", "amount_too_high")
                raise HTTPException(status_code=400, detail="Amount exceeds maximum limit")
            
            # Process payment
            payment_response = await simulate_payment_processing(payment_request)
            
            # Store in memory database
            payments_db[payment_response.payment_id] = payment_response
            
            # Record metrics
            payment_requests.add(1, {"method": "POST", "endpoint": "/payments"})
            payment_duration.record(time.time() - start_time, {"method": "POST", "endpoint": "/payments"})
            
            return payment_response
            
        except HTTPException:
            raise
        except Exception as e:
            span.record_exception(e)
            logger.error("Payment processing failed", error=str(e))
            raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/payments/{payment_id}", response_model=PaymentStatus)
async def get_payment_status(payment_id: str):
    """Get payment status by ID"""
    with tracer.start_as_current_span("get_payment_status") as span:
        span.set_attribute("payment.id", payment_id)
        
        if payment_id not in payments_db:
            span.set_attribute("error.type", "not_found")
            raise HTTPException(status_code=404, detail="Payment not found")
        
        payment = payments_db[payment_id]
        
        logger.info("Retrieved payment status", payment_id=payment_id, status=payment.status)
        
        return PaymentStatus(
            payment_id=payment.payment_id,
            status=payment.status,
            amount=payment.amount,
            processed_at=payment.processed_at,
            failure_reason=payment.failure_reason
        )

@app.get("/payments")
async def list_payments(user_id: Optional[str] = None, status: Optional[str] = None):
    """List payments with optional filtering"""
    with tracer.start_as_current_span("list_payments") as span:
        span.set_attribute("filter.user_id", user_id or "all")
        span.set_attribute("filter.status", status or "all")
        
        filtered_payments = []
        for payment in payments_db.values():
            # Apply filters
            if user_id and payment.order_id != user_id:  # Note: using order_id as proxy for user_id
                continue
            if status and payment.status != status:
                continue
            filtered_payments.append(payment)
        
        span.set_attribute("payments.count", len(filtered_payments))
        
        logger.info("Listed payments", 
                   count=len(filtered_payments),
                   user_id=user_id,
                   status=status)
        
        return {
            "payments": filtered_payments,
            "total": len(filtered_payments),
            "filters": {
                "user_id": user_id,
                "status": status
            }
        }

# Incident simulation endpoints for SRE training
@app.post("/simulate/failure")
async def simulate_failure():
    """Simulate payment service failure for incident response training"""
    global SERVICE_DOWN
    SERVICE_DOWN = True
    
    logger.warning("Payment service failure simulated for training")
    
    return {
        "message": "Payment service failure simulated",
        "service_down": True,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/simulate/recovery")
async def simulate_recovery():
    """Simulate service recovery"""
    global SERVICE_DOWN
    SERVICE_DOWN = False
    
    logger.info("Payment service recovery simulated")
    
    return {
        "message": "Payment service recovery simulated",
        "service_down": False,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/simulate/high-latency")
async def simulate_high_latency():
    """Simulate high latency scenario"""
    global LATENCY_SIMULATION
    LATENCY_SIMULATION = "high"
    
    logger.warning("High latency simulation enabled")
    
    return {
        "message": "High latency simulation enabled",
        "latency_mode": "high",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/simulate/normal-latency")
async def simulate_normal_latency():
    """Return to normal latency"""
    global LATENCY_SIMULATION
    LATENCY_SIMULATION = "normal"
    
    logger.info("Normal latency restored")
    
    return {
        "message": "Normal latency restored",
        "latency_mode": "normal",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/simulate/failure-rate")
async def set_failure_rate(rate: float):
    """Set payment failure rate (0.0 to 1.0)"""
    global FAILURE_RATE
    
    if not 0.0 <= rate <= 1.0:
        raise HTTPException(status_code=400, detail="Rate must be between 0.0 and 1.0")
    
    FAILURE_RATE = rate
    
    logger.warning("Payment failure rate updated", new_rate=rate)
    
    return {
        "message": f"Payment failure rate set to {rate}",
        "failure_rate": rate,
        "timestamp": datetime.now().isoformat()
    }

# Startup event
@app.on_event("startup")
async def startup_event():
    """Initialize service on startup"""
    logger.info("Payment Service starting up",
               failure_rate=FAILURE_RATE,
               latency_simulation=LATENCY_SIMULATION,
               service_down=SERVICE_DOWN)

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    
    logger.info("Starting Payment Service", host=host, port=port)
    
    uvicorn.run(
        "app:app",
        host=host,
        port=port,
        reload=os.getenv("DEBUG", "false").lower() == "true"
    )
