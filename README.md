# PyGate

A lightweight Python-based API Gateway / Reverse Proxy built to route requests to backend services with authentication, rate limiting, logging, retries, and config-driven routing.

## Planned Stack

- Python
- FastAPI
- httpx
- Uvicorn
- PyYAML
- Pydantic
- Docker
- Kubernetes
- C++ backend service

## Goal

Build a configurable async gateway that sits in front of multiple backend services, including a custom C++ HTTP server, and forwards requests based on route rules defined in a YAML config.

## Status

Initial project setup.

## Current Focus

The first milestone is to implement a minimal working reverse proxy: receive an HTTP request, match it against `config.yaml`, forward it to the correct backend, and return the backend response to the client.

This project is intended to demonstrate backend infrastructure fundamentals such as service-to-service communication, request routing, observability, and containerized deployment.
