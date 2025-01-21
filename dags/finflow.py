from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.google.cloud.transfers.local_to_gcs import LocalFilesystemToGCSOperator
from airflow.providers.google.cloud.transfers.gcs_to_bigquery import GCSToBigQueryOperator
from airflow.providers.google.cloud.operators.bigquery import BigQueryExecuteQueryOperator as BigQueryOperator
from airflow.utils.task_group import TaskGroup
from faker import Faker
import json
import os
import tempfile
import random
import pandas as pd
import itertools
from datetime import datetime, timedelta
import logging
from airflow.providers.google.cloud.operators.bigquery import BigQueryCreateEmptyDatasetOperator
from airflow.providers.google.cloud.operators.bigquery import BigQueryCheckOperator
from google.cloud import bigquery
from airflow.operators.bash import BashOperator
from datetime import datetime, timedelta
import time
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from airflow.models import BaseOperator
from airflow.utils.decorators import apply_defaults
import subprocess

# Configure logging
logger = logging.getLogger(__name__)

# DAG default arguments
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
}

# Configuration
GCS_BUCKET = 'finflow-analytics-data'
PROJECT_ID = 'finflow-analytics-production'
DATASET_ID = 'finflow_data'
BQ_LOCATION = 'us-central1'


# DBT Configuration
DBT_PROJECT_DIR = '/opt/dbt'
DBT_PROFILES_DIR = '/opt/dbt/profiles'

# Ensure directories exist
os.makedirs(DBT_PROJECT_DIR, exist_ok=True)
os.makedirs(DBT_PROFILES_DIR, exist_ok=True)

# Error classes
class DataGenerationError(Exception):
    """Custom error for data generation failures"""
    pass

class ReferentialIntegrityError(Exception):
    """Custom error for referential integrity violations"""
    pass

# Initialize sequence generators for keys
class KeyGenerator:
    _instance = None
    _initialized = False
    _counters = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(KeyGenerator, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self.initialize_counters()
            self.__class__._initialized = True
    
    def initialize_counters(self):
        """Initialize counters from max values in BigQuery"""
        try:
            # Get max keys using BigQuery operator
            max_keys_sql = f"""
            SELECT
                MAX(CASE WHEN table_name = 'customers' THEN max_key ELSE 0 END) as customer_max,
                MAX(CASE WHEN table_name = 'products' THEN max_key ELSE 0 END) as product_max,
                MAX(CASE WHEN table_name = 'locations' THEN max_key ELSE 0 END) as location_max,
                MAX(CASE WHEN table_name = 'accounts' THEN max_key ELSE 0 END) as account_max,
                MAX(CASE WHEN table_name = 'transactions' THEN max_key ELSE 0 END) as transaction_max
            FROM (
                SELECT 'customers' as table_name, COALESCE(MAX(customer_key), 0) as max_key 
                FROM `{PROJECT_ID}.{DATASET_ID}.src_customers`
                UNION ALL
                SELECT 'products', COALESCE(MAX(product_key), 0)
                FROM `{PROJECT_ID}.{DATASET_ID}.src_products`
                UNION ALL
                SELECT 'locations', COALESCE(MAX(location_key), 0)
                FROM `{PROJECT_ID}.{DATASET_ID}.src_locations`
                UNION ALL
                SELECT 'accounts', COALESCE(MAX(account_key), 0)
                FROM `{PROJECT_ID}.{DATASET_ID}.src_accounts`
                UNION ALL
                SELECT 'transactions', COALESCE(MAX(transaction_key), 0)
                FROM `{PROJECT_ID}.{DATASET_ID}.src_transactions`
            )
            """
            
            get_max_keys = BigQueryOperator(
                task_id='get_max_keys',
                sql=max_keys_sql,
                use_legacy_sql=False,
                dag=dag
            )
            
            result = get_max_keys.execute(context={})
            
            # Initialize counters with max values + 1
            self._counters = {
                'customer': itertools.count(result['customer_max'] + 1),
                'product': itertools.count(result['product_max'] + 1),
                'location': itertools.count(result['location_max'] + 1),
                'account': itertools.count(result['account_max'] + 1),
                'transaction': itertools.count(result['transaction_max'] + 1)
            }
        except Exception as e:
            logging.warning(f"Failed to get max keys, starting from 1: {str(e)}")
            # Fallback to starting from 1
            self._counters = {
                'customer': itertools.count(1),
                'product': itertools.count(1),
                'location': itertools.count(1),
                'account': itertools.count(1),
                'transaction': itertools.count(1)
            }
    
    def get_next_key(self, entity_type):
        try:
            return next(self._counters[entity_type])
        except KeyError:
            raise ValueError(f"Invalid entity type: {entity_type}")

key_gen = KeyGenerator()

def handle_data_generation(func):
    """Decorator for handling data generation errors"""
    def wrapper(*args, **kwargs):
        try:
            logger.info(f"Starting data generation for {func.__name__}")
            result = func(*args, **kwargs)
            logger.info(f"Successfully completed data generation for {func.__name__}")
            return result
        except Exception as e:
            logger.error(f"Error in data generation for {func.__name__}: {str(e)}")
            raise DataGenerationError(f"Failed to generate data in {func.__name__}: {str(e)}")
    return wrapper

def handle_timestamps(df):
    """Helper function to standardize timestamp handling"""
    timestamp_columns = ['created_at', 'updated_at', 'ingestion_timestamp']
    for col in timestamp_columns:
        if col in df.columns:
            # Convert to Unix timestamp (seconds since epoch)
            df[col] = pd.to_datetime(df[col]).astype('int64') // 10**9
    return df

@handle_data_generation
def generate_date_dimension(**context):
    """Generate date dimension source data"""
    start_date = datetime(2025, 1, 1)
    end_date = datetime(2025, 12, 31)
    dates = []
    
    current_date = start_date
    while current_date <= end_date:
        # Create timestamp without microseconds
        current_timestamp = datetime.utcnow().replace(microsecond=0)
        date_key = int(current_date.strftime('%Y%m%d'))
        
        date_record = {
            'date_key': date_key,
            'full_date': current_date.date(),
            'year': current_date.year,
            'quarter': (current_date.month - 1) // 3 + 1,
            'month': current_date.month,
            'day': current_date.day,
            'day_of_week': current_date.strftime('%A'),
            'is_weekend': current_date.weekday() >= 5,
            'is_holiday': (current_date.month, current_date.day) in [(1, 1), (7, 4), (12, 25)],
            'fiscal_year': f"FY{current_date.year}" if current_date.month < 4 else f"FY{current_date.year + 1}",
            'created_at': current_timestamp,
            'updated_at': current_timestamp,
            'ingestion_timestamp': current_timestamp
        }
        dates.append(date_record)
        current_date += timedelta(days=1)
    
    df = pd.DataFrame(dates)
    
    # Convert timestamp columns to datetime64[s] (seconds precision)
    timestamp_columns = ['created_at', 'updated_at', 'ingestion_timestamp']
    for col in timestamp_columns:
        df[col] = df[col].astype('datetime64[s]')
    
    temp_dir = tempfile.mkdtemp()
    filepath = os.path.join(temp_dir, 'dates.parquet')
    df.to_parquet(filepath, index=False)
    
    context['task_instance'].xcom_push(key='dates_file_path', value=filepath)
    context['task_instance'].xcom_push(key='date_keys', value=df['date_key'].tolist())
    return filepath

@handle_data_generation
def generate_customer_data(**context):
    """Generate customer source data"""
    fake = Faker()
    customers = []
    customer_keys = []
    used_customer_ids = set()
    
    segments = ['High Value', 'Medium Value', 'Standard']
    
    for _ in range(100):
        # Create timestamp without microseconds
        current_timestamp = datetime.utcnow().replace(microsecond=0)
        
        while True:
            customer_id = random.randint(100000, 999999)
            if customer_id not in used_customer_ids:
                used_customer_ids.add(customer_id)
                break
        
        customer_key = key_gen.get_next_key('customer')
        customer_keys.append(customer_key)
        
        customer = {
            'customer_key': customer_key,
            'customer_id': customer_id,
            'first_name': fake.first_name(),
            'last_name': fake.last_name(),
            'email': fake.email(),
            'phone_number': fake.phone_number(),
            'address': fake.street_address(),
            'city': fake.city(),
            'state': fake.state(),
            'country': fake.country(),
            'postal_code': fake.postcode(),
            'customer_segment': random.choice(segments),
            'customer_since_date': fake.date_between(start_date='-5y', end_date='today'),
            'total_accounts': random.randint(1, 5),
            'active_accounts': random.randint(1, 5),
            'total_transaction_volume': float(round(random.uniform(1000, 1000000), 2)),
            'last_transaction_date': fake.date_time_between(start_date='-1y', end_date='now'),
            'is_active': random.choice([True, False]),
            'created_at': current_timestamp,
            'updated_at': current_timestamp,
            'ingestion_timestamp': current_timestamp
        }
        customers.append(customer)
    
    df = pd.DataFrame(customers)
    
    # Convert timestamp columns to datetime64[s]
    timestamp_columns = ['created_at', 'updated_at', 'ingestion_timestamp', 'last_transaction_date']
    for col in timestamp_columns:
        df[col] = pd.to_datetime(df[col]).astype('datetime64[s]')
    
    temp_dir = tempfile.mkdtemp()
    filepath = os.path.join(temp_dir, 'customers.parquet')
    df.to_parquet(filepath, index=False)
    
    context['task_instance'].xcom_push(key='customers_file_path', value=filepath)
    context['task_instance'].xcom_push(key='customer_keys', value=customer_keys)
    return filepath

@handle_data_generation
def generate_product_data(**context):
    """Generate product source data"""
    products = []
    product_keys = []
    
    categories = ['Savings', 'Checking', 'Investment', 'Loan', 'Credit']
    subcategories = {
        'Savings': ['Basic', 'Premium', 'Youth', 'Senior'],
        'Checking': ['Standard', 'Premium', 'Business'],
        'Investment': ['Stocks', 'Bonds', 'Mutual Funds', 'ETFs'],
        'Loan': ['Personal', 'Home', 'Auto', 'Business'],
        'Credit': ['Standard', 'Gold', 'Platinum', 'Business']
    }
    
    for category in categories:
        for subcategory in subcategories[category]:
            # Create timestamp without microseconds
            current_timestamp = datetime.utcnow().replace(microsecond=0)
            
            product_key = key_gen.get_next_key('product')
            product_keys.append(product_key)
            
            product = {
                'product_key': product_key,
                'product_id': f"{category[:3].upper()}_{subcategory[:3].upper()}_{random.randint(1000, 9999)}",
                'product_name': f"{category} {subcategory}",
                'product_category': category,
                'product_subcategory': subcategory,
                'interest_rate': float(round(random.uniform(0.5, 15.0), 2)),
                'monthly_fee': float(round(random.uniform(0, 50.0), 2)),
                'is_active': random.choice([True, True, True, False]),
                'created_at': current_timestamp,
                'updated_at': current_timestamp,
                'ingestion_timestamp': current_timestamp
            }
            products.append(product)
    
    df = pd.DataFrame(products)
    
    # Convert timestamp columns to datetime64[s]
    timestamp_columns = ['created_at', 'updated_at', 'ingestion_timestamp']
    for col in timestamp_columns:
        df[col] = pd.to_datetime(df[col]).astype('datetime64[s]')
    
    temp_dir = tempfile.mkdtemp()
    filepath = os.path.join(temp_dir, 'products.parquet')
    df.to_parquet(filepath, index=False)
    
    context['task_instance'].xcom_push(key='product_keys', value=product_keys)
    context['task_instance'].xcom_push(key='products_file_path', value=filepath)
    return filepath

@handle_data_generation
def generate_location_data(**context):
    """Generate location source data"""
    fake = Faker()
    locations = []
    location_keys = []
    
    for _ in range(50):
        # Create timestamp without microseconds
        current_timestamp = datetime.utcnow().replace(microsecond=0)
        
        location_key = key_gen.get_next_key('location')
        location_keys.append(location_key)
        
        country_code = fake.country_code()
        country_name = fake.country()
        
        location = {
            'location_key': location_key,
            'location_id': f"LOC_{fake.unique.random_number(digits=6)}",
            'location_name': f"{fake.city()} Branch",
            'address': fake.street_address(),
            'city': fake.city(),
            'state': fake.state(),
            'region': fake.state(),
            'country': country_name, # remove from here and dbt bronze
            'country_name': country_name,
            'country_code': country_code,
            'currency_code': 'USD',
            'postal_code': fake.postcode(),
            'timezone': random.choice(['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles']),
            'latitude': float(fake.latitude()),
            'longitude': float(fake.longitude()),
            'created_at': current_timestamp,
            'updated_at': current_timestamp,
            'ingestion_timestamp': current_timestamp
        }
        locations.append(location)
    
    df = pd.DataFrame(locations)
    
    # Convert timestamp columns to datetime64[s]
    timestamp_columns = ['created_at', 'updated_at', 'ingestion_timestamp']
    for col in timestamp_columns:
        df[col] = pd.to_datetime(df[col]).astype('datetime64[s]')
    
    temp_dir = tempfile.mkdtemp()
    filepath = os.path.join(temp_dir, 'locations.parquet')
    df.to_parquet(filepath, index=False)
    
    context['task_instance'].xcom_push(key='location_keys', value=location_keys)
    context['task_instance'].xcom_push(key='locations_file_path', value=filepath)
    return filepath

@handle_data_generation
def generate_account_data(**context):
    """Generate account source data"""
    fake = Faker()
    accounts = []
    account_keys = []
    
    customer_keys = context['task_instance'].xcom_pull(key='customer_keys', task_ids='generate_customer_data')
    product_keys = context['task_instance'].xcom_pull(key='product_keys', task_ids='generate_product_data')
    
    for customer_key in customer_keys:
        num_accounts = random.randint(1, 3)
        
        for _ in range(num_accounts):
            # Create timestamp without microseconds
            current_timestamp = datetime.utcnow().replace(microsecond=0)
            
            account_key = key_gen.get_next_key('account')
            account_keys.append(account_key)
            
            account = {
                'account_key': account_key,
                'account_id': random.randint(100000, 999999),
                'customer_key': customer_key,
                'product_key': random.choice(product_keys),
                'account_type': random.choice(['Savings', 'Checking', 'Credit', 'Investment']),
                'account_status': random.choice(['Active', 'Inactive', 'Closed']),
                'initial_deposit': float(round(random.uniform(100, 10000), 2)),
                'minimum_balance': float(round(random.uniform(0, 1000), 2)),
                'balance': float(round(random.uniform(0, 100000), 2)),
                'credit_limit': float(round(random.uniform(1000, 50000), 2)),
                'opened_date': fake.date_between(start_date='-5y', end_date='today'),
                'closed_date': None,
                'created_at': current_timestamp,
                'updated_at': current_timestamp,
                'ingestion_timestamp': current_timestamp
            }
            accounts.append(account)
    
    df = pd.DataFrame(accounts)
    
    # Convert timestamp columns to datetime64[s]
    timestamp_columns = ['created_at', 'updated_at', 'ingestion_timestamp']
    for col in timestamp_columns:
        df[col] = pd.to_datetime(df[col]).astype('datetime64[s]')
    
    temp_dir = tempfile.mkdtemp()
    filepath = os.path.join(temp_dir, 'accounts.parquet')
    df.to_parquet(filepath, index=False)
    
    context['task_instance'].xcom_push(key='account_keys', value=account_keys)
    context['task_instance'].xcom_push(key='accounts_file_path', value=filepath)
    return filepath

@handle_data_generation
def generate_transaction_data(**context):
    """Generate transaction source data"""
    transactions = []
    transaction_keys = []
    
    account_keys = context['task_instance'].xcom_pull(key='account_keys', task_ids='generate_account_data')
    customer_keys = context['task_instance'].xcom_pull(key='customer_keys', task_ids='generate_customer_data')
    product_keys = context['task_instance'].xcom_pull(key='product_keys', task_ids='generate_product_data')
    location_keys = context['task_instance'].xcom_pull(key='location_keys', task_ids='generate_location_data')
    date_keys = context['task_instance'].xcom_pull(key='date_keys', task_ids='generate_date_data')
    
    for account_key in account_keys:
        num_transactions = random.randint(1, 5)
        
        for _ in range(num_transactions):
            # Create timestamp without microseconds
            current_timestamp = datetime.utcnow().replace(microsecond=0)
            
            transaction = {
                'transaction_id': random.randint(1000000, 9999999),
                'account_key': account_key,
                'customer_key': random.choice(customer_keys),
                'product_key': random.choice(product_keys),
                'location_key': random.choice(location_keys),
                'date_key': random.choice(date_keys),
                'transaction_type': random.choice(['Deposit', 'Withdrawal', 'Transfer', 'Payment']),
                'transaction_amount': float(round(random.uniform(10, 5000), 2)),
                'fee_amount': float(round(random.uniform(0, 50), 2)),
                'transaction_status': random.choice(['Completed', 'Completed', 'Completed', 'Pending', 'Failed']),
                'created_at': current_timestamp,
                'updated_at': current_timestamp,
                'ingestion_timestamp': current_timestamp
            }
            transactions.append(transaction)
    
    df = pd.DataFrame(transactions)
    
    # Convert timestamp columns to datetime64[s]
    timestamp_columns = ['created_at', 'updated_at', 'ingestion_timestamp']
    for col in timestamp_columns:
        df[col] = pd.to_datetime(df[col]).astype('datetime64[s]')
    
    temp_dir = tempfile.mkdtemp()
    filepath = os.path.join(temp_dir, 'transactions.parquet')
    df.to_parquet(filepath, index=False)
    
    context['task_instance'].xcom_push(key='transaction_keys', value=transaction_keys)
    context['task_instance'].xcom_push(key='transactions_file_path', value=filepath)
    return filepath

# Define separate merge templates for different table types
MERGE_SQL_TEMPLATE = """
-- Create the target table if it doesn't exist
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.src_{table}s` (
    {schema_definition}
);

-- Perform the merge operation
MERGE `{project}.{dataset}.src_{table}s` T
USING (
    SELECT *
    FROM `{project}.{dataset}.src_{table}s_temp`
) S
ON {merge_condition}
WHEN MATCHED THEN
    UPDATE SET
        {update_columns}
WHEN NOT MATCHED THEN
    INSERT ({columns})
    VALUES ({source_columns})
"""

# Define merge conditions for each table
MERGE_CONDITIONS = {
    'date': 'T.date_key = S.date_key',
    'customer': 'T.customer_key = S.customer_key',
    'product': 'T.product_key = S.product_key',
    'location': 'T.location_key = S.location_key',
    'account': 'T.account_key = S.account_key',
    'transaction': 'T.transaction_id = S.transaction_id'
}

# Modify the merge validation SQL to be a template that we'll format for each table
MERGE_VALIDATION_SQL_TEMPLATE = """
WITH pre_counts AS (
    SELECT 
        '{table}s' as table_name,
        COUNT(*) as pre_merge_count
    FROM `{project}.{dataset}.src_{table}s`
    WHERE DATE(ingestion_timestamp) < DATE('{{{{ ds }}}}')
),
post_counts AS (
    SELECT 
        '{table}s' as table_name,
        COUNT(*) as post_merge_count
    FROM `{project}.{dataset}.src_{table}s`
    WHERE DATE(ingestion_timestamp) <= DATE('{{{{ ds }}}}')
),
new_records AS (
    SELECT 
        '{table}s' as table_name,
        COUNT(*) as new_record_count
    FROM `{project}.{dataset}.src_{table}s`
    WHERE DATE(ingestion_timestamp) = DATE('{{{{ ds }}}}')
)
SELECT
    CASE 
        WHEN post_counts.post_merge_count < pre_counts.pre_merge_count 
        THEN ERROR('Data loss detected in {table}s table')
        WHEN post_counts.post_merge_count - pre_counts.pre_merge_count != new_records.new_record_count
        THEN ERROR('Unexpected record count after merge in {table}s table')
        ELSE 'Merge validation successful for {table}s table'
    END as validation_result
FROM pre_counts
JOIN post_counts ON pre_counts.table_name = post_counts.table_name
JOIN new_records ON pre_counts.table_name = new_records.table_name
"""

def ensure_dataset_exists(**context):
    """Ensure BigQuery dataset exists before running tasks"""
    client = bigquery.Client(project=PROJECT_ID)
    dataset_id = f"{PROJECT_ID}.{DATASET_ID}"
    
    try:
        # Try to get the dataset
        dataset = client.get_dataset(dataset_id)
        print(f"Dataset {dataset_id} already exists")
    except Exception:
        # Create the dataset if it doesn't exist
        dataset = bigquery.Dataset(dataset_id)
        dataset.location = BQ_LOCATION
        dataset = client.create_dataset(dataset, exists_ok=True)
        print(f"Created dataset {dataset_id}")
    
    # Verify the dataset exists
    try:
        client.get_dataset(dataset_id)
        print(f"Successfully verified dataset {dataset_id} exists")
        return True
    except Exception as e:
        print(f"Failed to verify dataset: {str(e)}")
        raise

class DbtBaseOperator(BaseOperator):
    """
    Custom operator for dbt commands
    """
    template_fields = ("vars",)

    @apply_defaults
    def __init__(
        self,
        dbt_command,
        dbt_project_dir="/opt/dbt",
        profiles_dir="/opt/dbt/profiles",
        vars=None,
        *args,
        **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.dbt_command = dbt_command
        self.dbt_project_dir = dbt_project_dir
        self.profiles_dir = profiles_dir
        self.vars = vars or {}

    def execute(self, context):
        """Execute dbt command"""
        os.chdir(self.dbt_project_dir)
        command = f"dbt {self.dbt_command} --profiles-dir {self.profiles_dir}"
        
        if self.vars:
            vars_str = " ".join([f"--vars '{k}:{v}'" for k, v in self.vars.items()])
            command = f"{command} {vars_str}"

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True
        )

        self.log.info(result.stdout)
        
        if result.returncode != 0:
            raise Exception(f"dbt command failed: {result.stderr}")

        return result.stdout

# SQL to verify source data quality
verify_source_sql = """
WITH table_counts AS (
    SELECT 'customers' as table_name, COUNT(*) as record_count 
    FROM `{project}.{dataset}.src_customers`
    UNION ALL
    SELECT 'products', COUNT(*) 
    FROM `{project}.{dataset}.src_products`
    UNION ALL
    SELECT 'locations', COUNT(*) 
    FROM `{project}.{dataset}.src_locations`
    UNION ALL
    SELECT 'accounts', COUNT(*) 
    FROM `{project}.{dataset}.src_accounts`
    UNION ALL
    SELECT 'transactions', COUNT(*) 
    FROM `{project}.{dataset}.src_transactions`
    UNION ALL
    SELECT 'dates', COUNT(*) 
    FROM `{project}.{dataset}.src_dates`
)
SELECT
    CASE 
        WHEN MIN(record_count) = 0 THEN 
            ERROR('One or more source tables are empty')
        ELSE 'All source tables contain data'
    END as validation_result
FROM table_counts;
""".format(project=PROJECT_ID, dataset=DATASET_ID)

# SQL to verify referential integrity
dq_check_sql = """
WITH integrity_checks AS (
    -- Check customer references
    SELECT 'Invalid customer reference in accounts' as check_name,
    COUNT(*) as invalid_count
    FROM `{project}.{dataset}.src_accounts` a
    LEFT JOIN `{project}.{dataset}.src_customers` c
    ON a.customer_key = c.customer_key
    WHERE c.customer_key IS NULL
    
    UNION ALL
    
    -- Check product references
    SELECT 'Invalid product reference in accounts',
    COUNT(*)
    FROM `{project}.{dataset}.src_accounts` a
    LEFT JOIN `{project}.{dataset}.src_products` p
    ON a.product_key = p.product_key
    WHERE p.product_key IS NULL
    
    UNION ALL
    
    -- Check account references in transactions
    SELECT 'Invalid account reference in transactions',
    COUNT(*)
    FROM `{project}.{dataset}.src_transactions` t
    LEFT JOIN `{project}.{dataset}.src_accounts` a
    ON t.account_key = a.account_key
    WHERE a.account_key IS NULL
    
    UNION ALL
    
    -- Check location references in transactions
    SELECT 'Invalid location reference in transactions',
    COUNT(*)
    FROM `{project}.{dataset}.src_transactions` t
    LEFT JOIN `{project}.{dataset}.src_locations` l
    ON t.location_key = l.location_key
    WHERE l.location_key IS NULL
)
SELECT
    CASE 
        WHEN MAX(invalid_count) > 0 THEN
            ERROR(CONCAT('Referential integrity violation found: ', 
                  STRING_AGG(CONCAT(check_name, ': ', CAST(invalid_count AS STRING)), '; ')))
        ELSE 'All referential integrity checks passed'
    END as validation_result
FROM integrity_checks
WHERE invalid_count > 0;
""".format(project=PROJECT_ID, dataset=DATASET_ID)

# Define schemas for each table type
TABLE_SCHEMAS = {
    'date': [
        {'name': 'date_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'full_date', 'type': 'DATE', 'mode': 'REQUIRED'},
        {'name': 'year', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'quarter', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'month', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'day', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'day_of_week', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'is_weekend', 'type': 'BOOLEAN', 'mode': 'REQUIRED'},
        {'name': 'is_holiday', 'type': 'BOOLEAN', 'mode': 'REQUIRED'},
        {'name': 'fiscal_year', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'created_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'updated_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'ingestion_timestamp', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'}
    ],
    'customer': [
        {'name': 'customer_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'customer_id', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'first_name', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'last_name', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'email', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'phone_number', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'address', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'city', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'state', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'country', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'postal_code', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'customer_segment', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'customer_since_date', 'type': 'DATE', 'mode': 'REQUIRED'},
        {'name': 'total_accounts', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'active_accounts', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'total_transaction_volume', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'last_transaction_date', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'is_active', 'type': 'BOOLEAN', 'mode': 'REQUIRED'},
        {'name': 'created_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'updated_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'ingestion_timestamp', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'}
    ],
    'product': [
        {'name': 'product_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'product_id', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'product_name', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'product_category', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'product_subcategory', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'interest_rate', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'monthly_fee', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'is_active', 'type': 'BOOLEAN', 'mode': 'REQUIRED'},
        {'name': 'created_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'updated_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'ingestion_timestamp', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'}
    ],
    'location': [
        {'name': 'location_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'location_id', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'location_name', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'address', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'city', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'state', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'region', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'country', 'type': 'STRING', 'mode': 'REQUIRED'}, # remove from here and dbt bronze
        {'name': 'country_name', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'country_code', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'currency_code', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'postal_code', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'timezone', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'latitude', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'longitude', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'created_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'updated_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'ingestion_timestamp', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'}
    ],
    'account': [
        {'name': 'account_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'account_id', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'customer_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'product_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'account_type', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'account_status', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'initial_deposit', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'minimum_balance', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'balance', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'credit_limit', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'opened_date', 'type': 'DATE', 'mode': 'REQUIRED'},
        {'name': 'closed_date', 'type': 'DATE', 'mode': 'NULLABLE'},
        {'name': 'created_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'updated_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'ingestion_timestamp', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'}
    ],
    'transaction': [
        {'name': 'transaction_id', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'account_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'customer_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'product_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'location_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'date_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
        {'name': 'transaction_type', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'transaction_amount', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'fee_amount', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
        {'name': 'transaction_status', 'type': 'STRING', 'mode': 'REQUIRED'},
        {'name': 'created_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'updated_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
        {'name': 'ingestion_timestamp', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'}
    ]
}

# Create DAG
with DAG(
    'finflow_source_data_pipeline',
    default_args=default_args,
    description='FinFlow Source Data Generation Pipeline',
    schedule_interval=timedelta(days=1),
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags=['fintech', 'source-data'],
) as dag:
    
    # Initialize key generator
    init_key_generator = PythonOperator(
        task_id='init_key_generator',
        python_callable=lambda **context: KeyGenerator().initialize_counters(),
        provide_context=True
    )
    
    # Ensure dataset exists
    ensure_dataset = PythonOperator(
        task_id='ensure_dataset_exists',
        python_callable=ensure_dataset_exists,
        provide_context=True,
        retries=3,
        retry_delay=timedelta(seconds=5)
    )

    # Create data generation tasks
    generate_date_data = PythonOperator(
        task_id='generate_date_data',
        python_callable=generate_date_dimension,
    )
    
    generate_location_data = PythonOperator(
        task_id='generate_location_data',
        python_callable=generate_location_data,
    )
    
    generate_product_data = PythonOperator(
        task_id='generate_product_data',
        python_callable=generate_product_data,
    )
    
    generate_customer_data = PythonOperator(
        task_id='generate_customer_data',
        python_callable=generate_customer_data,
    )

    generate_account_data = PythonOperator(
        task_id='generate_account_data',
        python_callable=generate_account_data,
    )
    
    generate_transaction_data = PythonOperator(
        task_id='generate_transaction_data',
        python_callable=generate_transaction_data,
    )

    # Define task groups first
    with TaskGroup(group_id='load_to_gcs_bq') as load_tasks:
        task_name_mapping = {
            'date': 'generate_date_data',
            'location': 'generate_location_data',
            'product': 'generate_product_data',
            'customer': 'generate_customer_data',
            'account': 'generate_account_data',
            'transaction': 'generate_transaction_data'
        }

        for table in ['date', 'location', 'product', 'customer', 'account', 'transaction']:
            # Generate schema DDL string
            schema_ddl = ',\n    '.join([
                f"{field['name']} {field['type']}" + 
                (f" NOT NULL" if field['mode'] == 'REQUIRED' else "") 
                for field in TABLE_SCHEMAS[table]
            ])
            
            # Generate column lists for merge operation
            columns = ', '.join([field['name'] for field in TABLE_SCHEMAS[table]])
            source_columns = ', '.join([f'S.{field["name"]}' for field in TABLE_SCHEMAS[table]])
            update_columns = ', '.join([
                f'{field["name"]} = S.{field["name"]}'
                for field in TABLE_SCHEMAS[table]
                if field["name"] not in ['created_at']  # Don't update created_at
            ])

            upload_to_gcs = LocalFilesystemToGCSOperator(
                task_id=f'upload_{table}_to_gcs',
                src=f"{{{{ task_instance.xcom_pull(task_ids='{task_name_mapping[table]}', key='{table}s_file_path') }}}}",
                dst=f'raw/{table}s/{{{{ data_interval_end.strftime("%Y-%m-%d") }}}}/{table}s.parquet',
                bucket=GCS_BUCKET,
            )

            load_to_temp = GCSToBigQueryOperator(
                task_id=f'load_{table}_to_temp',
                bucket=GCS_BUCKET,
                source_objects=[f'raw/{table}s/{{{{ ds }}}}/{table}s.parquet'],
                destination_project_dataset_table=f'{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp',
                source_format='PARQUET',
                write_disposition='WRITE_TRUNCATE',
                create_disposition='CREATE_IF_NEEDED',
                schema_fields=TABLE_SCHEMAS[table],
                location=BQ_LOCATION,
                autodetect=False,
                time_partitioning=None,
                cluster_fields=None,
                allow_quoted_newlines=True,
                encoding='UTF-8',
                ignore_unknown_values=True,
                max_bad_records=0
            )

            if table == 'customer':
                schema_fields = [
                    {'name': 'customer_key', 'type': 'INTEGER', 'mode': 'REQUIRED'},
                    {'name': 'customer_id', 'type': 'INTEGER', 'mode': 'REQUIRED'},
                    {'name': 'first_name', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'last_name', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'email', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'phone_number', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'address', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'city', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'state', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'country', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'postal_code', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'customer_segment', 'type': 'STRING', 'mode': 'REQUIRED'},
                    {'name': 'customer_since_date', 'type': 'DATE', 'mode': 'REQUIRED'},
                    {'name': 'total_accounts', 'type': 'INTEGER', 'mode': 'REQUIRED'},
                    {'name': 'active_accounts', 'type': 'INTEGER', 'mode': 'REQUIRED'},
                    {'name': 'total_transaction_volume', 'type': 'FLOAT64', 'mode': 'REQUIRED'},
                    {'name': 'last_transaction_date', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
                    {'name': 'is_active', 'type': 'BOOLEAN', 'mode': 'REQUIRED'},
                    {'name': 'created_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
                    {'name': 'updated_at', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'},
                    {'name': 'ingestion_timestamp', 'type': 'TIMESTAMP', 'mode': 'REQUIRED'}
                ]
            elif table == 'date':
                transform_timestamps = BigQueryOperator(
                    task_id=f'transform_{table}_timestamps',
                    sql=f"""
                    CREATE OR REPLACE TABLE `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp` AS
                    SELECT 
                        date_key,
                        full_date,
                        year,
                        quarter,
                        month,
                        day,
                        day_of_week,
                        is_weekend,
                        is_holiday,
                        fiscal_year,
                        created_at,
                        updated_at,
                        ingestion_timestamp
                    FROM `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp`
                    """,
                    use_legacy_sql=False,
                    location=BQ_LOCATION
                )
            elif table == 'location':
                transform_timestamps = BigQueryOperator(
                    task_id=f'transform_{table}_timestamps',
                    sql=f"""
                    CREATE OR REPLACE TABLE `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp` AS
                    SELECT 
                        location_key,
                        location_id,
                        location_name,
                        address,
                        city,
                        state,
                        region,
                        country, 
                        country_name,
                        country_code,
                        currency_code,
                        postal_code,
                        timezone,
                        latitude,
                        longitude,
                        created_at,
                        updated_at,
                        ingestion_timestamp
                    FROM `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp`
                    """,
                    use_legacy_sql=False,
                    location=BQ_LOCATION
                )
            elif table == 'product':
                transform_timestamps = BigQueryOperator(
                    task_id=f'transform_{table}_timestamps',
                    sql=f"""
                    CREATE OR REPLACE TABLE `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp` AS
                    SELECT 
                        product_key,
                        product_id,
                        product_name,
                        product_category,
                        product_subcategory,
                        interest_rate,
                        monthly_fee,
                        is_active,
                        created_at,
                        updated_at,
                        ingestion_timestamp
                    FROM `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp`
                    """,
                    use_legacy_sql=False,
                    location=BQ_LOCATION
                )
            elif table == 'account':
                transform_timestamps = BigQueryOperator(
                    task_id=f'transform_{table}_timestamps',
                    sql=f"""
                    CREATE OR REPLACE TABLE `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp` AS
                    SELECT 
                        account_key,
                        account_id,
                        customer_key,
                        product_key,
                        account_type,
                        account_status,
                        initial_deposit,
                        minimum_balance,
                        balance,
                        credit_limit,
                        opened_date,
                        closed_date,
                        created_at,
                        updated_at,
                        ingestion_timestamp
                    FROM `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp`
                    """,
                    use_legacy_sql=False,
                    location=BQ_LOCATION
                )
            elif table == 'transaction':
                transform_timestamps = BigQueryOperator(
                    task_id=f'transform_{table}_timestamps',
                    sql=f"""
                    CREATE OR REPLACE TABLE `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp` AS
                    SELECT 
                        transaction_id,
                        account_key,
                        customer_key,
                        product_key,
                        location_key,
                        date_key,
                        transaction_type,
                        transaction_amount,
                        fee_amount,
                        transaction_status,
                        created_at,
                        updated_at,
                        ingestion_timestamp
                    FROM `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp`
                    """,
                    use_legacy_sql=False,
                    location=BQ_LOCATION
                )

            merge_to_main = BigQueryOperator(
                task_id=f'merge_{table}_to_main',
                sql=MERGE_SQL_TEMPLATE.format(
                    project=PROJECT_ID,
                    dataset=DATASET_ID,
                    table=table,
                    schema_definition=schema_ddl,
                    merge_condition=MERGE_CONDITIONS[table],
                    columns=columns,
                    source_columns=source_columns,
                    update_columns=update_columns
                ),
                use_legacy_sql=False,
                location=BQ_LOCATION
            )

            cleanup_temp = BigQueryOperator(
                task_id=f'cleanup_{table}_temp',
                sql=f'DROP TABLE IF EXISTS `{PROJECT_ID}.{DATASET_ID}.src_{table}s_temp`',
                use_legacy_sql=False
            )

            # Set up task dependencies
            upload_to_gcs >> load_to_temp >> transform_timestamps >> merge_to_main >> cleanup_temp

    with TaskGroup(group_id='data_quality_checks') as quality_checks:
        verify_source_data = BigQueryOperator(
            task_id='verify_source_data',
            sql=verify_source_sql,
            use_legacy_sql=False,
        )

        verify_referential_integrity = BigQueryOperator(
            task_id='verify_referential_integrity',
            sql=dq_check_sql,
            use_legacy_sql=False,
        )

        # Create a merge validation task for each table
        merge_validation_tasks = []
        for table in ['date', 'location', 'product', 'customer', 'account', 'transaction']:
            merge_validation = BigQueryOperator(
                task_id=f'verify_merge_results_{table}',
                sql=MERGE_VALIDATION_SQL_TEMPLATE.format(
                    table=table,
                    project=PROJECT_ID,
                    dataset=DATASET_ID
                ),
                use_legacy_sql=False,
            )
            merge_validation_tasks.append(merge_validation)

    # Add this after the quality_checks TaskGroup and before the final dependencies

    with TaskGroup(group_id='dbt_transformations') as dbt_tasks:
        # Clean stale models first
        dbt_clean = DbtBaseOperator(
            task_id='dbt_clean',
            dbt_command='clean',
            dbt_project_dir=DBT_PROJECT_DIR,
            profiles_dir=DBT_PROFILES_DIR
        )

        # Install dependencies after cleaning
        dbt_deps = DbtBaseOperator(
            task_id='dbt_deps',
            dbt_command='deps',
            dbt_project_dir=DBT_PROJECT_DIR,
            profiles_dir=DBT_PROFILES_DIR,
            retries=2
        )

        # Run with full-refresh for incremental models
        dbt_run = DbtBaseOperator(
            task_id='dbt_run',
            dbt_command='run --full-refresh',
            dbt_project_dir=DBT_PROJECT_DIR,
            profiles_dir=DBT_PROFILES_DIR
        )

        # Run data quality tests
        dbt_test = DbtBaseOperator(
            task_id='dbt_test',
            dbt_command='test',
            dbt_project_dir=DBT_PROJECT_DIR,
            profiles_dir=DBT_PROFILES_DIR
        )

        # Set the dependency chain
        dbt_clean >> dbt_deps >> dbt_run >> dbt_test


        # Set up dependencies for the quality checks
        verify_source_data >> verify_referential_integrity >> merge_validation_tasks

    # Update main DAG dependencies to include dataset creation
    init_key_generator >> ensure_dataset >> [
        generate_date_data,
        generate_customer_data,
        generate_product_data,
        generate_location_data
    ]

    [
        generate_date_data,
        generate_customer_data,
        generate_product_data,
        generate_location_data
    ] >> generate_account_data >> generate_transaction_data >> load_tasks >> quality_checks >> dbt_tasks
