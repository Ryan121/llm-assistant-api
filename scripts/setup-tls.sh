#!/bin/bash
# Helper script for TLS certificate management
# Usage: ./scripts/setup-tls.sh [production|development]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TLS_DIR="$PROJECT_ROOT/deploy/nginx-tls/ssl"
CERTBOT_DIR="$PROJECT_ROOT/deploy/nginx-tls/certbot"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

setup_development_certs() {
    log_info "Generating self-signed certificates for development..."
    
    mkdir -p "$TLS_DIR"
    
    # Generate self-signed certificate
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$TLS_DIR/key.pem" \
        -out "$TLS_DIR/cert.pem" \
        -subj "/CN=localhost/O=LLM-Assistant-Dev/C=US" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
    
    # Set permissions
    chmod 600 "$TLS_DIR/key.pem"
    chmod 644 "$TLS_DIR/cert.pem"
    
    log_info "Certificates generated:"
    log_info "  Certificate: $TLS_DIR/cert.pem"
    log_info "  Private Key: $TLS_DIR/key.pem"
    log_warn ""
    log_warn "WARNING: These are self-signed certificates for development only!"
    log_warn "Do not use in production."
    log_warn ""
    log_info "To trust the certificate locally, add it to your system trust store:"
    log_info "  macOS:  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain $TLS_DIR/cert.pem"
    log_info "  Linux:  sudo cp $TLS_DIR/cert.pem /usr/local/share/ca-certificates/ && sudo update-ca-certificates"
    log_info "  Windows: Import cert.pem into Trusted Root Certification Authorities"
}

setup_production_letsencrypt() {
    local domain="${1:-}"
    
    if [[ -z "$domain" ]]; then
        log_error "Domain name required for Let's Encrypt certificate"
        echo "Usage: $0 production <your-domain.com>"
        exit 1
    fi
    
    log_info "Setting up Let's Encrypt certificate for $domain..."
    
    # Create directories for certbot
    mkdir -p "$CERTBOT_DIR/www"
    mkdir -p "$CERTBOT_DIR/conf"
    
    # Check if certbot is installed
    if ! command -v certbot &> /dev/null; then
        log_error "certbot is not installed. Please install it first:"
        log_info "  Ubuntu/Debian: sudo apt-get install certbot"
        log_info "  macOS: brew install certbot"
        log_info "  Or use Docker: docker run -it --rm certbot/certbot"
        exit 1
    fi
    
    # Obtain certificate
    log_info "Requesting certificate from Let's Encrypt..."
    sudo certbot certonly --standalone \
        -d "$domain" \
        -d "www.$domain" \
        --email "admin@$domain" \
        --agree-tos \
        --non-interactive \
        --cert-path "$CERTBOT_DIR/conf/live/$domain/fullchain.pem" \
        --key-path "$CERTBOT_DIR/conf/live/$domain/privkey.pem"
    
    # Create symlinks in SSL directory
    mkdir -p "$TLS_DIR"
    ln -sf "$CERTBOT_DIR/conf/live/$domain/fullchain.pem" "$TLS_DIR/cert.pem"
    ln -sf "$CERTBOT_DIR/conf/live/$domain/privkey.pem" "$TLS_DIR/key.pem"
    
    log_info "Certificate obtained successfully!"
    log_info "Certificate will auto-renew with: sudo certbot renew"
    
    # Setup auto-renewal cron job
    if ! crontab -l | grep -q 'certbot renew'; then
        log_info "Setting up automatic certificate renewal..."
        (crontab -l 2>/dev/null || echo ""; echo "0 3 * * * /usr/bin/certbot renew --quiet") | crontab -
    fi
}

setup_production_custom() {
    log_info "Setting up custom certificate..."
    log_info "Please place your certificate files in:"
    log_info "  Certificate: $TLS_DIR/cert.pem"
    log_info "  Private Key: $TLS_DIR/key.pem"
    log_info ""
    log_info "Expected files:"
    log_info "  - cert.pem (certificate chain)"
    log_info "  - key.pem (private key)"
    log_info ""
    log_info "After placing the files, run:"
    log_info "  chmod 600 $TLS_DIR/key.pem"
    log_info "  chmod 644 $TLS_DIR/cert.pem"
    
    mkdir -p "$TLS_DIR"
    
    # Wait for user to place files
    read -p "Press Enter after you've placed the certificate files..."
    
    if [[ ! -f "$TLS_DIR/cert.pem" ]] || [[ ! -f "$TLS_DIR/key.pem" ]]; then
        log_error "Certificate files not found!"
        exit 1
    fi
    
    # Verify certificate
    if openssl x509 -in "$TLS_DIR/cert.pem" -text -noout > /dev/null 2>&1; then
        log_info "Certificate verified successfully!"
        openssl x509 -in "$TLS_DIR/cert.pem" -text -noout | grep -E "(Subject:|Not Before|Not After)"
    else
        log_error "Certificate verification failed!"
        exit 1
    fi
}

check_existing_certs() {
    if [[ -f "$TLS_DIR/cert.pem" ]] && [[ -f "$TLS_DIR/key.pem" ]]; then
        log_info "Existing certificates found:"
        if openssl x509 -in "$TLS_DIR/cert.pem" -text -noout > /dev/null 2>&1; then
            openssl x509 -in "$TLS_DIR/cert.pem" -text -noout | grep -E "(Subject:|Not Before|Not After)"
        fi
        echo ""
        read -p "Overwrite existing certificates? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log_info "Keeping existing certificates."
            exit 0
        fi
    fi
}

main() {
    local mode="${1:-development}"
    
    echo "========================================"
    echo "LLM Assistant API - TLS Setup"
    echo "========================================"
    echo ""
    
    case "$mode" in
        development|dev)
            check_existing_certs
            setup_development_certs
            ;;
        production|prod)
            local domain="${2:-}"
            echo "Production certificate setup:"
            echo "  1. Let's Encrypt (free, auto-renewing)"
            echo "  2. Custom certificate (your own CA)"
            echo ""
            read -p "Choose option [1/2]: " cert_choice
            
            check_existing_certs
            
            case "$cert_choice" in
                1)
                    if [[ -z "$domain" ]]; then
                        read -p "Enter your domain name: " domain
                    fi
                    setup_production_letsencrypt "$domain"
                    ;;
                2)
                    setup_production_custom
                    ;;
                *)
                    log_error "Invalid choice"
                    exit 1
                    ;;
            esac
            ;;
        *)
            log_error "Unknown mode: $mode"
            echo "Usage: $0 [development|production] [domain]"
            echo ""
            echo "Examples:"
            echo "  $0 development                    # Self-signed for local dev"
            echo "  $0 production example.com         # Let's Encrypt"
            echo "  $0 production                     # Custom certificate"
            exit 1
            ;;
    esac
    
    echo ""
    log_info "Setup complete!"
    echo ""
    echo "Next steps:"
    echo "  1. Review/update nginx.conf for your domain"
    echo "  2. Start nginx: docker compose --profile nginx up -d"
    echo "  3. Test: curl -k https://localhost/healthz"
    echo ""
}

main "$@"
