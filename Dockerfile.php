# Dockerfile.php — extends the base agent with a PHP 8.4 + Composer toolchain.
# Use when the agent needs to run Laravel/Symfony tooling (Pint, PHPUnit,
# Pest, PHPStan, Artisan migrations, etc.) inside the container.
#
# PHP 8.4 isn't in Debian Bookworm's main repos, so we add the official
# packages.sury.org repo (the PHP team's recommended source for Debian).
#
# Build:
#   docker build -t superpos-agent-claude-php -f Dockerfile.php .

FROM slim-apiary-agent-base

USER root

# Add sury's PHP repo + install PHP 8.4 with the Laravel-relevant extensions:
#   - mbstring, xml, tokenizer  — framework core
#   - curl, sqlite3, mysql, pgsql  — HTTP + DB drivers
#   - bcmath, gd, zip, intl  — common composer-package requirements
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://packages.sury.org/php/apt.gpg | \
        gpg --dearmor -o /etc/apt/keyrings/sury-php.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/sury-php.gpg] https://packages.sury.org/php/ $(lsb_release -sc) main" \
        > /etc/apt/sources.list.d/sury-php.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        php8.4-cli \
        php8.4-mbstring \
        php8.4-xml \
        php8.4-tokenizer \
        php8.4-curl \
        php8.4-sqlite3 \
        php8.4-mysql \
        php8.4-pgsql \
        php8.4-bcmath \
        php8.4-gd \
        php8.4-zip \
        php8.4-intl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Composer — pinned to the official endpoint.
RUN curl -sS https://getcomposer.org/installer | \
    php -- --install-dir=/usr/local/bin --filename=composer

USER agent
