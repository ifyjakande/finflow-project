#!/bin/bash

# Get the external IP
EXTERNAL_IP=$(curl -s http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip -H "Metadata-Flavor: Google")

# Update the docker-compose environment
sed -i "s|AIRFLOW__WEBSERVER__BASE_URL=.*|AIRFLOW__WEBSERVER__BASE_URL=http://${EXTERNAL_IP}:8080|g" .env

# Restart Airflow webserver to apply changes
docker-compose restart airflow-webserver
