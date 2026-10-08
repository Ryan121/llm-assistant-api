# TLS Termination with nginx

This directory contains example configurations for adding TLS termination in front of the LLM Assistant API gateway.

## Why TLS Termination?

The gateway binds to `0.0.0.0:8081` inside the container and is published to the host. In production, you should:

1. **Never expose the gateway directly** - Always put it behind a reverse proxy
2. **Terminate TLS at the proxy** - The gateway doesn't handle TLS itself
3. **Use strong ciphers** - Modern TLS 1.2/1.3 only
4. **Enable HSTS** - Force HTTPS for all clients

## Quick Start

### 1. Generate or obtain certificates

For production, use Let's Encrypt or your CA:

```bash
# Let's Encrypt (requires certbot)
sudo certbot certonly --standalone -d your-domain.com
```

For testing, generate self-signed certificates:

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout nginx/ssl/selfsigned.key \
  -out nginx/ssl/selfsigned.crt \
  -subj "/CN=localhost"
```

### 2. Update environment variables

```bash
# .env
API_PORT=8081  # Internal port, nginx will expose 443
```

### 3. Start with nginx

```bash
# Using docker-compose with nginx profile
docker compose --profile nginx up -d

# Or manually
docker compose up -d
docker compose --profile nginx up -d nginx
```

### 4. Verify

```bash
# Test HTTPS endpoint
curl -k https://localhost/healthz

# Test with certificate verification (if using real certs)
curl https://your-domain.com/healthz

# Check certificate
openssl s_client -connect localhost:443 -servername localhost
```

## Configuration Files

### `nginx.conf`

Production-ready nginx configuration with:
- TLS 1.2 and 1.3 only
- Strong cipher suites
- HSTS header
- Rate limiting at proxy level
- Proper proxy headers for the gateway
- Access logging with request IDs
- Health check endpoints

### `docker-compose.nginx.yml`

Docker Compose override that adds nginx service with:
- Certificate volume mounts
- Port 80/443 exposure
- Dependency on gateway service
- Automatic restart policy

## Security Considerations

### Certificate Management

- **Production**: Use Let's Encrypt with auto-renewal
- **Internal**: Consider internal CA or Vault PKI
- **Testing**: Self-signed is fine, but don't use in production

### Rate Limiting

The nginx config includes basic rate limiting (10 req/s per IP). Adjust based on your needs:

```nginx
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;
```

### Access Control

For additional security, consider:
- IP whitelisting in nginx
- Client certificate authentication (mTLS)
- API key validation (already in gateway)

### Monitoring

nginx logs access to `/var/log/nginx/access.log`. Key fields:
- `$request_id` - Correlates with gateway's `x-request-id`
- `$status` - HTTP status code
- `$upstream_response_time` - Gateway response time

## Troubleshooting

### Certificate errors

```bash
# Check certificate validity
openssl x509 -in nginx/ssl/cert.pem -text -noout

# Verify certificate chain
openssl verify -CAfile nginx/ssl/ca-chain.pem nginx/ssl/cert.pem
```

### Connection refused

```bash
# Check nginx is running
docker compose ps nginx

# Check nginx logs
docker compose logs nginx

# Test gateway directly (bypass nginx)
curl http://localhost:8081/healthz
```

### 502 Bad Gateway

```bash
# Gateway might not be ready
docker compose logs gateway

# Check network connectivity
docker compose exec nginx ping gateway
```

## Advanced Configurations

### Multiple domains

Edit `nginx.conf` to add multiple `server` blocks for different domains.

### WebSocket support

The gateway doesn't use WebSockets, but if you add them later:

```nginx
location /ws {
    proxy_pass http://gateway:8081;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

### Load balancing

For multiple gateway instances:

```nginx
upstream gateway_backend {
    least_conn;
    server gateway1:8081;
    server gateway2:8081;
    server gateway3:8081;
}
```

## Performance Tuning

### Worker processes

```nginx
worker_processes auto;  # Use all CPU cores
worker_connections 4096;  # Increase for high concurrency
```

### Buffering

```nginx
proxy_buffering off;  # Disable for streaming responses
proxy_request_buffering off;
```

### Keepalive

```nginx
upstream gateway_backend {
    server gateway:8081;
    keepalive 32;  # Persistent connections to gateway
}
```

## Resources

- [nginx TLS Configuration](https://wiki.mozilla.org/Security/Server_Side_TLS#Nginx_recommendations)
- [Let's Encrypt](https://letsencrypt.org/)
- [SSL Labs Test](https://www.ssllabs.com/ssltest/)
