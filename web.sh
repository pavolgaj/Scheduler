#!/bin/bash
#run on waitress-serve production server
gunicorn -b 0.0.0.0:5000 -k gevent -w 1 --threads 20 -t 0 --reload web:app
