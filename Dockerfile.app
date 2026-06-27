FROM nginx:alpine
COPY app/build/web/ /usr/share/nginx/html/
COPY nginx.app.conf /etc/nginx/conf.d/default.conf
CMD sh -c "sed -i \"s/PORT_PLACEHOLDER/${PORT:-80}/\" /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"
