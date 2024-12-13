#!/bin/bash
#run on waitress-serve production server
waitress-serve --host 0.0.0.0 --port=5000 web:app
