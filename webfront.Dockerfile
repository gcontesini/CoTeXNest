# The "webfront" container: a thin static-file + reverse-proxy layer,
# deliberately with no Python/TeX Live/git inside it. It owns none of
# the application's logic (rendering/saving happen in the "backend"
# container, built from ./Dockerfile) -- it only serves
# `src/webapp/static/` and forwards `/api/*` to that backend over the
# docker-compose network. See nginx.conf for the routing.
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf

# Two copies of the same tree: one at the web root (so "/" resolves
# index.html), one under /static (so the app's own "/static/js/..."
# and "/static/css/..." references, already used identically by
# webapp.main's StaticFiles mount in the non-Docker dev workflow,
# resolve the same way here).
COPY src/webapp/static /usr/share/nginx/html/static
COPY src/webapp/static/index.html /usr/share/nginx/html/index.html

EXPOSE 80
