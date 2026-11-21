# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /src
COPY requirements.lock /opt/requirements.lock
RUN pip install --no-cache-dir -r /opt/requirements.lock
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tgz
RUN tar xzf /tmp/sqlglot.tgz --strip-components=1 && rm /tmp/sqlglot.tgz
COPY build_profile.py /opt/build_profile.py
ENV PYTHONPATH=/src SKIP_INTEGRATION=0
RUN python /opt/build_profile.py /opt/profile.json.gz
FROM scratch AS export
COPY --from=0 /opt/profile.json.gz /profile.json.gz
