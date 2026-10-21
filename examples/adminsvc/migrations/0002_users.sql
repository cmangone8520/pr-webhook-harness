ALTER TABLE users DROP COLUMN email;
ALTER TABLE users RENAME COLUMN name TO full_name;
ALTER TABLE users ADD COLUMN tenant_id integer NOT NULL;
CREATE INDEX idx_users_tenant ON users (tenant_id);
