FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS profiler
COPY build_profile.py type_coverage.py prepare.py requirements.lock /opt/
RUN mkdir -p /opt/out && (timeout -k 5 465 python /opt/prepare.py || true)
FROM scratch AS export
COPY --from=profiler /opt/out/profile.json.gz /profile.json.gz
