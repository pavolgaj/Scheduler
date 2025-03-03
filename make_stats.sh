#!/bin/bash

outname=`python3 make_stats.py`

rm mail/attachments/*

cp "statistics/statistics_"$outname".pdf" mail-stats/attachments/
cp "statistics/statistics_"$outname".csv" mail-stats/attachments/   

python3 send_mail.py mail-stats

