# The bench image: the project's test image plus the two DBAPI drivers
# Home Assistant's recorder needs for `mysql://` and `postgresql://` URLs.
#
# Built by `script/bench`; `.devcontainer/Dockerfile` is deliberately left
# alone so the ordinary suite keeps running on exactly the image CI uses.
FROM ha-discrete-stats-test

USER root
# HA's recorder falls back from MySQLdb to pymysql for a `mysql://` URL,
# and uses psycopg2 for `postgresql://`. sqlalchemy_utils is already a
# test requirement; the loader also runs in this image.
RUN pip install --no-cache-dir pymysql psycopg2-binary
USER vscode
