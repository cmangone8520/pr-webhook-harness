FROM node:latest
ENV API_TOKEN=supersecretvalue123
ADD https://example.com/install.sh /tmp/install.sh
RUN curl -sSL https://example.com/setup.sh | sh
COPY . /app
CMD ["node", "/app/server.js"]
