FROM nginx:alpine
COPY app/build/web/ /usr/share/nginx/html/
RUN printf 'server {\n  listen $PORT;\n  root /usr/share/nginx/html;\n  index index.html;\n  location / { try_files $uri $uri/ /index.html; }\n}\n' > /etc/nginx/templates/default.conf.template
ENV PORT=80
EXPOSE 80
