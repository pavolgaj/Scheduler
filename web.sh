#!/bin/bash
#run on waitress-serve production server
gunicorn web:app -b 0.0.0.0:5000 -k gevent -w 20 --threads 20 -t 0 --reload --reload-extra-file lasilla_config.txt $(find templates -type f -name '*' -exec echo --reload-extra-file {} \;)
