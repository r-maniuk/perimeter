# syntax=docker/dockerfile:1.7
# PostgreSQL 18 with PostGIS 3.6, this deployment's access rules and its first-start role setup.
# Tuning lives in docker-compose.yml, next to the connection pools it is sized for.
# The official postgis/postgis images are published for amd64 only; imresamu/postgis builds the
# same docker-postgis recipes for amd64 and arm64 (github.com/ImreSamu/docker-postgis), so the
# stack runs natively on ARM laptops and servers. Pinned by digest.
FROM imresamu/postgis:18-3.6.1-alpine3.23@sha256:3c51493252f45d654ef07bf68ae22c9cec52a196edd0eaaa67ba06b654e56a01

# The upstream first-start script loads PostGIS, topology and the TIGER geocoder into a template and
# every database; ours creates only the extensions the schema uses, in the application database.
RUN rm /docker-entrypoint-initdb.d/10_postgis.sh \
 && install -d -m 0755 /etc/postgresql
COPY --chmod=0444 pg_hba.conf /etc/postgresql/pg_hba.conf
COPY --chmod=0444 initdb/10-perimeter.sql /docker-entrypoint-initdb.d/10-perimeter.sql

# The data volume is created from /var/lib/postgresql, which the image gives to this user; running
# as it from the start means the entrypoint never needs root.
USER postgres
