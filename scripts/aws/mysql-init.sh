#!/bin/bash
set -eu

mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE USER 'anipick_app'@'%' IDENTIFIED BY '${ANIPICK_MYSQL_PASSWORD}' REQUIRE SSL;
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
ON \`${MYSQL_DATABASE}\`.* TO 'anipick_app'@'%';
SQL
