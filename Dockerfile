FROM apache/airflow:2.8.1

USER root

# Install git and ssh for private repos if needed
RUN apt-get update && apt-get install -y git

# Remove existing directory if it exists and clone the dbt repo
RUN rm -rf /opt/dbt && \
    git clone https://github.com/ifyjakande/finflow-analytics-dbt.git /opt/dbt

# Create profiles directory and set permissions (do this as root)
RUN mkdir -p /opt/dbt/profiles && \
    chown -R airflow:root /opt/dbt

# Switch to airflow user for remaining operations
USER airflow

# Install requirements including dbt
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# Copy profiles.yml into the container (updated path)
COPY dbt/profiles/profiles.yml /opt/dbt/profiles/profiles.yml

# Verify dbt project setup
RUN ls -la /opt/dbt && \
    ls -la /opt/dbt/profiles && \
    cat /opt/dbt/profiles/profiles.yml
