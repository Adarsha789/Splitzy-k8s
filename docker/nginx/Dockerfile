# Stage 1: Build frontend
FROM node:20-slim AS react

ENV APP_HOME=/frontend
WORKDIR $APP_HOME


# Copy package definition files
COPY frontend/package*.json ./

# Install all dependencies (clean, reproducible install from the lockfile)
RUN npm ci

# Copying rest of code
COPY frontend .

# API base URL is baked at build time. Default to a same-origin relative path
# ("/api") so the browser hits the same domain and the Ingress routes /api to the
# backend — no CORS, and Secure/SameSite=None cookies work correctly.
ARG VITE_API_URL=/api
ENV VITE_API_URL=$VITE_API_URL

# For react build
RUN npm run build

# Stage 2: Nginx to serve the build
FROM nginx:1.27-alpine

# Remove default config
RUN rm /etc/nginx/conf.d/default.conf

# Copy custom Nginx config
COPY docker/nginx/default.conf /etc/nginx/conf.d/default.conf

# Copy the build output from the previous stage
COPY --from=react /frontend/dist /usr/share/nginx/html

# Expose port
EXPOSE 80


