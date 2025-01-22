# FinFlow Analytics 🏦

FinFlow Analytics is a modern data pipeline that processes financial transaction data using Apache Airflow, Google Cloud Platform (GCP), and dbt. The project implements a complete data warehouse solution for financial data analytics.

## Architecture Overview 🏗️

![FinFlow Analytics Architecture](architecture.png)

The architecture consists of three main components:

1. **Data Generation Layer** 🔄
   - Apache Airflow hosted on GCP Compute VM using Docker
   - Generates synthetic financial data including customers, accounts, transactions, and more
   - Orchestrates the entire data pipeline

2. **Data Storage Layer** 💾
   - Google Cloud Storage (GCS) for raw data storage
   - BigQuery for data warehousing
   - Handles both raw and transformed data

3. **Data Transformation Layer** ⚙️
   - dbt for data modeling and transformations
   - Source code available at [finflow-analytics-dbt](https://github.com/ifyjakande/finflow-analytics-dbt)
   - Implements a modern data warehouse model

## Required GCP IAM Roles 🔑

For the service account, you need to grant the following roles:

### Google Cloud Storage Roles
- `roles/storage.objectViewer` - Read access to GCS objects
- `roles/storage.objectCreator` - Create new GCS objects
- `roles/storage.admin` - Full access to GCS buckets and objects

### BigQuery Roles
- `roles/bigquery.dataEditor` - Read/write access to BigQuery data
- `roles/bigquery.jobUser` - Permission to run BigQuery jobs
- `roles/bigquery.dataOwner` - Full access to BigQuery datasets and tables

### Additional Required Roles
- `roles/compute.viewer` - View Compute Engine resources
- `roles/logging.viewer` - View logs
- `roles/monitoring.viewer` - View monitoring data

## Data Model 📊

![FinFlow Data Model](data_model.png)

The data warehouse follows a dimensional modeling approach with:

- Fact tables: transactions, customer metrics, account balances
- Dimension tables: customer, product, location, account, date
- Optimized for analytical queries and reporting

## Prerequisites 📋

- Docker and Docker Compose 🐳
- Google Cloud Platform account with:
  - Compute Engine
  - Cloud Storage
  - BigQuery
  - Service Account with appropriate permissions
- Python 3.8+ 🐍
- dbt

## Setup Instructions 🚀

1. **Clone the Repository**
   ```bash
   git clone <repository-url>
   cd finflow-analytics
   ```

2. **Configure Environment Variables**
   ```bash
   cp .env.example .env
   ```
   Update the following variables in `.env`:
   - `AIRFLOW_UID`
   - `_AIRFLOW_WWW_USER_USERNAME`
   - `_AIRFLOW_WWW_USER_PASSWORD`
   - GCP-related configurations

3. **Set Up Google Cloud Service Account** 🔐
   - Create a service account with necessary permissions
   - Download the JSON key file
   - Place it in the `config/google/` directory
   - Update the path in `docker-compose.yml`

4. **Start the Services**
   ```bash
   docker-compose up -d
   ```

5. **Initialize dbt**
   ```bash
   cd dbt
   dbt deps
   dbt seed
   ```

## Project Structure 📁

```
finflow-analytics/
├── dags/                 # Airflow DAG definitions
├── logs/                 # Airflow logs
├── config/              
│   └── google/          # GCP service account keys
├── dbt/
│   ├── models/          # dbt transformation models
│   └── profiles/        # dbt connection profiles
├── docker-compose.yml   # Docker services configuration
├── requirements.txt     # Python dependencies
└── README.md
```

## DAG Structure 📈

The main DAG (`finflow.py`) includes:

1. Data Generation Tasks
   - Generates synthetic data for all dimensions and facts
   - Implements data quality checks and validations

2. Loading Tasks
   - Uploads data to Google Cloud Storage
   - Loads data into BigQuery staging tables

3. Transformation Tasks
   - Executes dbt models
   - Performs data quality tests
   - Creates final analytical tables

## Data Pipeline 🔄

The pipeline follows these steps:

1. Generate synthetic financial data
2. Upload data to GCS in Parquet format
3. Load data into BigQuery staging tables
4. Transform data using dbt models
5. Perform data quality checks
6. Create final analytical tables

## Monitoring and Maintenance 🔍

- Access Airflow UI at `http://<your-vm-ip>:8080`
- Monitor DAG runs and task status
- View logs in the Airflow UI or `/logs` directory
- Check dbt documentation for transformation details

## Additional Resources 📚

- [dbt Models Repository](https://github.com/ifyjakande/finflow-analytics-dbt)
- [Apache Airflow Documentation](https://airflow.apache.org/docs/)
- [dbt Documentation](https://docs.getdbt.com/)

## Contributing 🤝

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## Support 💬

For support or questions, please open an issue in the repository.
