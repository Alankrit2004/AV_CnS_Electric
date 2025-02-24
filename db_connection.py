import psycopg2
from psycopg2.extras import RealDictCursor
import os
from dotenv import load_dotenv

def connect_to_database():
    """
    Establishes a new database connection for each request.
    """
    try:
        connection = psycopg2.connect(
            dbname="postgres",
            user="postgres.qfgubwgzlkdckpaugrcq",
            password="GexLiP4Mm8tv1oQQ",
            host="aws-0-ap-southeast-1.pooler.supabase.com",
            port="5432",
            cursor_factory=RealDictCursor
        )
        return connection
    except Exception as e:
        print(f"Failed to connect to the database: {e}")
        return None

