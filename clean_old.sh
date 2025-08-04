#!/bin/bash
# clean old schedules
find schedules/*.csv -mtime +60 -exec rm {} \;
find schedules/*.png -mtime +60 -exec rm {} \;
