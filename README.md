# PropertyIQ

PropertyIQ — Property Intelligence Platform.

## Production architecture

FastAPI
    |
    v
PostgreSQL / PostGIS
    |
    v
Supabase

## Required environment variable

DATABASE_URL

The production application reads DATABASE_URL from the
runtime environment.

Do not hard-code database credentials into main.py.

## Docker

The included Dockerfile starts:

uvicorn main:app --host 0.0.0.0 --port $PORT

## Health check

/api/v1/health

## Render

Use the Docker runtime and provide DATABASE_URL as a
private environment variable.

## Railway

Deploy the Dockerfile and provide DATABASE_URL through
Railway Variables.

## Important

The Supabase database already contains the PropertyIQ
PostgreSQL/PostGIS schema and existing data.

This deployment package does not contain database credentials.
