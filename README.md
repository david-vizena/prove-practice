# SRE Interview Prep: Production-Ready Microservices Platform

## Project Overview
This project demonstrates core SRE competencies through a realistic e-commerce microservices system with full observability, deployed on Kubernetes.

## Architecture
- **3 Microservices**: Product Service (Python), Order Service (Go), Payment Service (Python)
- **Observability**: OpenTelemetry, Prometheus, Grafana, Jaeger
- **Infrastructure**: Kubernetes (kind), Terraform, Auto-scaling
- **Incident Response**: Simulated failures with debugging practice

## Quick Start

### Prerequisites
```bash
# Install Docker Desktop
# Install kubectl
# Install kind (Kubernetes in Docker)
# Install helm
```

### Setup Commands
```bash
# Create kind cluster
kind create cluster --name sre-interview

# Install observability stack
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add jaegertracing https://jaegertracing.github.io/helm-charts
helm repo update

# Deploy monitoring stack
kubectl create namespace monitoring
helm install prometheus prometheus-community/kube-prometheus-stack -n monitoring
helm install jaeger jaegertracing/jaeger -n monitoring
```

## Learning Path
1. **Foundation Setup** - K8s cluster + observability stack
2. **Microservices** - Build services with OpenTelemetry
3. **K8s Deployment** - Deploy with auto-scaling
4. **Observability** - Dashboards, alerts, SLOs
5. **Terraform** - Infrastructure as Code
6. **Incident Response** - Failure simulation and debugging

## Interview Talking Points
- OpenTelemetry implementation across Python/Go services
- Kubernetes deployment with auto-scaling and health checks
- Terraform modules for infrastructure provisioning
- Incident response using distributed tracing
- SLO-based alerting and monitoring
