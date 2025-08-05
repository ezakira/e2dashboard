FROM python:3.11-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99 \
    HOME=/home/appuser

# Install dependencies including xauth and unzip
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates xvfb unzip xauth \
        libasound2 libatk1.0-0 libc6 libcairo2 libcups2 \
        libdbus-1-3 libexpat1 libfontconfig1 libgbm1 libgcc1 \
        libgdk-pixbuf2.0-0 libglib2.0-0 libgtk-3-0 libnspr4 \
        libpango-1.0-0 libpangocairo-1.0-0 libstdc++6 libx11-6 \
        libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 libxdamage1 \
        libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 libxss1 \
        libxtst6 libnss3 && \
    wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && \
    dpkg -i google-chrome-stable_current_amd64.deb || apt-get install -yf && \
    rm google-chrome-stable_current_amd64.deb && \
    rm -rf /var/lib/apt/lists/*

# Sanity check
RUN google-chrome-stable --version

# Download matching ChromeDriver
RUN FULL=$(google-chrome-stable --version \
      | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+') && \
    echo "Downloading matching CfT ChromeDriver $FULL…" && \
    wget -qO /tmp/chromedriver.zip \
      "https://storage.googleapis.com/chrome-for-testing-public/$FULL/linux64/chromedriver-linux64.zip" && \
    unzip -j /tmp/chromedriver.zip "chromedriver-linux64/chromedriver" -d /usr/local/bin/ && \
    chmod +x /usr/local/bin/chromedriver && \
    rm /tmp/chromedriver.zip

# Environment variable for WebDriver Manager
ENV WDM_CACHE_PATH=""

# Create non-root user
RUN groupadd -r appuser && \
    useradd -r -g appuser -m -d /home/appuser appuser

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt
COPY . .

USER appuser

CMD ["sh", "-c", "xvfb-run --auto-servernum --server-args='-screen 0 1920x1080x24' python bot.py"]