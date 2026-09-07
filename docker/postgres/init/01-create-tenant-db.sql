SELECT 'CREATE DATABASE kibak_tenant_test OWNER kibak_local'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'kibak_tenant_test')\gexec
