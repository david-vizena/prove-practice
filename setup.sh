#!/bin/bash

# SRE Interview Prep - Setup Script
# This script sets up the local Kubernetes cluster and observability stack

set -e  # Exit on any error

echo "🚀 Starting SRE Interview Prep Setup..."

# Check prerequisites
echo "📋 Checking prerequisites..."

if ! command -v docker &> /dev/null; then
    echo "❌ Docker is not installed. Please install Docker Desktop first."
    exit 1
fi

if ! command -v kubectl &> /dev/null; then
    echo "❌ kubectl is not installed. Please install kubectl first."
    exit 1
fi

if ! command -v kind &> /dev/null; then
    echo "❌ kind is not installed. Please install kind first."
    exit 1
fi

if ! command -v helm &> /dev/null; then
    echo "❌ helm is not installed. Please install helm first."
    exit 1
fi

echo "✅ All prerequisites are installed!"

# Check if Docker is running
if ! docker info &> /dev/null; then
    echo "❌ Docker is not running. Please start Docker Desktop."
    exit 1
fi

echo "✅ Docker is running!"

# Create kind cluster
echo "🔧 Creating Kubernetes cluster with kind..."
if kind get clusters | grep -q sre-interview; then
    echo "⚠️  Cluster 'sre-interview' already exists. Deleting it first..."
    kind delete cluster --name sre-interview
fi

kind create cluster --name sre-interview --config kind-config.yaml

echo "✅ Kubernetes cluster created!"

# Wait for cluster to be ready
echo "⏳ Waiting for cluster to be ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=300s

# Add Helm repositories
echo "📦 Adding Helm repositories..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo add jaegertracing https://jaegertracing.github.io/helm-charts
helm repo update

# Create monitoring namespace
echo "🏗️  Creating monitoring namespace..."
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -

# Deploy Prometheus stack
echo "📊 Deploying Prometheus and Grafana..."
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
    -n monitoring \
    -f k8s/monitoring/prometheus-values.yaml \
    --wait

# Deploy Jaeger
echo "🔍 Deploying Jaeger for distributed tracing..."
helm upgrade --install jaeger jaegertracing/jaeger \
    -n monitoring \
    -f k8s/monitoring/jaeger-values.yaml \
    --wait

# Wait for all pods to be ready
echo "⏳ Waiting for all monitoring pods to be ready..."
kubectl wait --for=condition=Ready pods --all -n monitoring --timeout=300s

# Display access information
echo ""
echo "🎉 Setup complete! Here's how to access your services:"
echo ""
echo "📊 Grafana: http://localhost:3000"
echo "   Username: admin"
echo "   Password: admin123"
echo ""
echo "📈 Prometheus: http://localhost:9090"
echo ""
echo "🔍 Jaeger: http://localhost:16686"
echo ""
echo "🔧 Useful commands:"
echo "   kubectl get pods -n monitoring"
echo "   kubectl get services -n monitoring"
echo "   kubectl logs -f deployment/prometheus-grafana -n monitoring"
echo ""
echo "Next steps:"
echo "1. Open Grafana and explore the dashboards"
echo "2. Check Prometheus targets and metrics"
echo "3. Start building your microservices!"
