#! /usr/bin/env python3
# -*- coding: utf-8 -*-

#
# Author: Jan Fuchs <fuky@asu.cas.cz>
#
# Copyright (C) 2020-2023 Astronomical Institute, Academy Sciences of the Czech Republic, v.v.i.
#
# modified by P. Gajdos

import os
import sys
import smtplib
import traceback
import configparser

from email.header import Header
from email.utils import formatdate
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

class SendMail:

    def __init__(self,cc,directory=None,check=False):
        self.attachments = {}
        self.message=""
            
        if directory is None: 
            self.load_cfg()            
        else:
            self.load_cfg(directory+'/conf')
            self.load_message(directory+'/message')
            for name in os.listdir(directory+'/attachments'):
                self.load_attachments(directory+'/attachments/'+name)
            
        self.mail["cc"]=cc
        
        if check:
            print("Only check")
            self.check()
            print("\nSUCCESS")
            sys.exit()       
        

    def load_cfg(self,filename = "mail/conf"):       
        if not os.path.isfile(filename):
            print("ERROR: Configuration file '%s' not found" % filename)
            sys.exit()

        rcp = configparser.ConfigParser()
        rcp.read(filename, encoding="utf-8")

        self.mail = dict(rcp.items("mail"))

        self.mail["to"] = ", ".join(self.mail["to"].strip().split())

    def load_message(self,template):
        filename = template

        if not os.path.isfile(filename):
            print("ERROR: Message file '%s' not found" % filename)
            sys.exit()

        with open(filename, "r", encoding="utf-8") as fo:
            lines = fo.readlines()

        self.message = "".join(lines)

    def load_attachments(self,filename):
        if os.path.isfile(filename):
            self.attachments[os.path.basename(filename)] = filename

    def check(self):
        print("\nConfiguration:\n")
        for item in ["smtp", "from", "to", "cc", "subject"]:
            print("    %s = %s" % (item, self.mail[item]))

        print("\nMessage:\n")
        print(self.message)

        print("\nAttachments:\n")
        for key in self.attachments:
            print("%s = %s" % (key, self.attachments[key]))

    def run(self):
        self.send_mail(self.mail["subject"], self.message, self.attachments)

    def send_mail(self, subject, text, attachments=None):
        msg = MIMEMultipart()

        msg["From"] = self.mail["from"]
        msg["To"] = self.mail["to"]
        msg["Cc"] = self.mail["cc"]
        msg["Subject"] = Header(subject, "utf-8")
        msg["Date"] = formatdate(localtime=True)
        msg['reply-to'] = self.mail["to"]+', '+self.mail["cc"]

        plain_text = MIMEText(text, "plain", "utf-8")
        msg.attach(plain_text)

        if attachments is not None:
            for key in attachments:                
                attachment_octet_stream = MIMEBase("application", "octet-stream")

                with open(attachments[key], "rb") as fo:
                    attachment_octet_stream.set_payload(fo.read())

                encoders.encode_base64(attachment_octet_stream)
                attachment_octet_stream.add_header("content-disposition", "attachment", filename=key)
                msg.attach(attachment_octet_stream)

        smtp = smtplib.SMTP(self.mail["smtp"])
        smtp.send_message(msg)
        #smtp.sendmail(self.mail["from"], self.mail["to"].split(", "), msg.as_string())
        smtp.quit()

def main():

    argv_len = len(sys.argv)

    if argv_len not in [2, 3]:
        print("Usage: %s DIRECTORY [c|check]" % sys.argv[0])
        return

    directory = sys.argv[1]
    check = False

    if argv_len == 3:
        check = True

    send_mail = SendMail(cc='',directory=directory,check=check)

    try:
        send_mail.run()
    except:
        traceback.print_exc()
        send_mail.send_mail("ERROR: exception", traceback.format_exc())

if __name__ == '__main__':
    main()
