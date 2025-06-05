from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle,Paragraph
from reportlab.lib.units import inch, mm
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.rl_config import defaultPageSize
from reportlab.lib.pagesizes import A4
from reportlab.platypus import PageBreak
from reportlab.platypus import Image
from reportlab.lib.utils import ImageReader
import datetime
import matplotlib.pyplot as plt
import io

# based on https://github.com/JohnFunkCode/dataframetopdf

class PDFReport(object):
    def __init__(self,name,nights=0):
        self.doc = SimpleDocTemplate(name, pagesize=A4)
        self.docElements = []
        self.nights=nights   #total duration of nights
        #setup the package scoped global variables we need
        now = datetime.datetime.now()
        PDFReport.timestamp = now.strftime("%Y-%m-%d %H:%M")
        PDFReport.sourcefile = "not initialized"
        PDFReport.pageinfo = "not initialized"
        PDFReport.Title = "not initialized"
        PDFReport.PAGE_HEIGHT = defaultPageSize[1];
        PDFReport.PAGE_WIDTH = defaultPageSize[0]
        PDFReport.styles = getSampleStyleSheet()   #sample style sheet doesn't seem to be used

    def set_title(self,title):
        self.doc.title = title

    def set_author(self,author):
        self.doc.author = author

    @staticmethod
    def set_pageInfo(pageinfo):
        PDFReport.pageinfo = pageinfo

    @staticmethod
    def set_sourcefile(sourcefile):
        PDFReport.sourcefile = sourcefile

    def put_dataframe_on_pdfpage(self, df):
        #table with observations
        elements = []
        
        def fig2image(f):
            #save image to buffer
            buf = io.BytesIO()
            f.savefig(buf, format='png', dpi=300)
            buf.seek(0)
            x, y = f.get_size_inches()
            return Image(buf, x * inch, y * inch)

        # Data Frame
        if df is not None:
            #create stats. by Supervisor, program, target
            sups={}
            supsT={}
            objs={}
            objsT={}
            prog={}
            progT={}
            for i in range(1,len(df)):
                #print(df[i])
                name=df[i][1]
                if name in sups: 
                    sups[name]+=df[i][3]
                    supsT[name]+=df[i][5]
                else: 
                    sups[name]=df[i][3]
                    supsT[name]=df[i][5]
                name=df[i][2]
                if name in prog: 
                    prog[name]+=df[i][3]
                    progT[name]+=df[i][5]
                else: 
                    prog[name]=df[i][3]
                    progT[name]=df[i][5]
                objs[df[i][0]]=df[i][3]
                objsT[df[i][0]]=df[i][5]
            
            #select only 1st 10 with biggest time/nights 
            sups10={}
            i=0
            n=10
            for x in sorted(sups, key=lambda x: sups[x])[::-1]:
                if i<n: sups10[x]=sups[x]
                elif 'other' in sups10: sups10['other']+=sups[x]
                else: sups10['other']=sups[x]
                i+=1
            
            objs10={}
            i=0
            n=15
            for x in sorted(objs, key=lambda x: objs[x])[::-1]:
                if i<n: objs10[x]=objs[x]
                elif 'other' in objs10: objs10['other']+=objs[x]
                else: objs10['other']=objs[x]
                i+=1
                
            supsT10={}
            allTime=0
            i=0
            n=10
            for x in sorted(supsT, key=lambda x: supsT[x])[::-1]:
                if i<n: supsT10[x]=supsT[x]
                elif 'other' in supsT10: supsT10['other']+=supsT[x]
                else: supsT10['other']=supsT[x]
                i+=1
                allTime+=supsT[x]
            
            objsT10={}
            i=0
            n=15
            for x in sorted(objsT, key=lambda x: objsT[x])[::-1]:
                if i<n: objsT10[x]=objsT[x]
                elif 'other' in objsT10: objsT10['other']+=objsT[x]
                else: objsT10['other']=objsT[x]
                i+=1
            
            
            styles = getSampleStyleSheet()
            title_style = styles['Heading3']
            title_style.alignment = 1
            
            center_style = styles['Normal']
            center_style.alignment = 1
            
            #main table
            t = Table(df)
            t.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), "Helvetica"),
                                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                                ('BACKGROUND',(0,0), (-1,0),colors.silver),
                                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                                ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
                                ('BOX', (0, 0), (-1, -1), 0.25, colors.black)]))
            elements.append(t)
            
            elements.append(Spacer(1, 0.25 * inch))
            p = Paragraph("Total observing time: %.1f hours" %allTime,center_style)
            elements.append(p)
            if self.nights>0:
                p = Paragraph("Total available time (between astro. twilights): %.1f hours" %self.nights,center_style)
                elements.append(p)
                p = Paragraph("Effectivity: %.2f%%" %(100*allTime/self.nights),center_style)
                elements.append(p)
            
            elements.append(Spacer(1, 0.2 * inch))
            elements.append(PageBreak())
        
            #statistics by Supervisor
            p = Paragraph("Observing nights",title_style)
            elements.append(p)
            
            df=[['Supervisor','Nights']]
            for x in sups10:
                df.append([x,sups10[x]])
                
            t = Table(df)
            t.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), "Helvetica"),
                                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                                ('BACKGROUND',(0,0), (-1,0),colors.silver),
                                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                                ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
                                ('BOX', (0, 0), (-1, -1), 0.25, colors.black)]))
            elements.append(t)
            
            elements.append(Spacer(1, 0.5 * inch))
            p = Paragraph("Observing time",title_style)
            elements.append(p)
            
            df=[['Supervisor','TotalTime (h)','Fraction']]
            for x in supsT10:
                df.append([x,round(supsT10[x],2),'%.2f%%' %(100*supsT10[x]/allTime)])
                
            t = Table(df)
            t.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), "Helvetica"),
                                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                                ('BACKGROUND',(0,0), (-1,0),colors.silver),
                                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                                ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
                                ('BOX', (0, 0), (-1, -1), 0.25, colors.black)]))
            elements.append(t)
                        
            elements.append(Spacer(1, 0.25 * inch))
            p = Paragraph("Total observing time: %.1f hours" %allTime,center_style)
            elements.append(p)
            if self.nights>0:
                p = Paragraph("Total available time (between astro. twilights): %.1f hours" %self.nights,center_style)
                elements.append(p)
                p = Paragraph("Effectivity: %.2f%%" %(100*allTime/self.nights),center_style)
                elements.append(p)
            
            elements.append(Spacer(1, 0.2 * inch))
            elements.append(PageBreak())
            
            #statistics by program
            p = Paragraph("Observing nights",title_style)
            elements.append(p)
            
            df=[['Program','Nights']]
            for x in prog:
                df.append([x,prog[x]])
                
            t = Table(df)
            t.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), "Helvetica"),
                                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                                ('BACKGROUND',(0,0), (-1,0),colors.silver),
                                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                                ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
                                ('BOX', (0, 0), (-1, -1), 0.25, colors.black)]))
            elements.append(t)
            
            elements.append(Spacer(1, 0.5 * inch))
            p = Paragraph("Observing time",title_style)
            elements.append(p)
            
            df=[['Program','TotalTime (h)','Fraction']]
            for x in progT:
                df.append([x,round(progT[x],2),'%.2f%%' %(100*progT[x]/allTime)])
                
            t = Table(df)
            t.setStyle(TableStyle([('FONTNAME', (0, 0), (-1, -1), "Helvetica"),
                                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                                ('BACKGROUND',(0,0), (-1,0),colors.silver),
                                ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
                                ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.black),
                                ('BOX', (0, 0), (-1, -1), 0.25, colors.black)]))
            elements.append(t)
                        
            elements.append(Spacer(1, 0.25 * inch))
            p = Paragraph("Total observing time: %.1f hours" %allTime,center_style)
            elements.append(p)
            if self.nights>0:
                p = Paragraph("Total available time (between astro. twilights): %.1f hours" %self.nights,center_style)
                elements.append(p)
                p = Paragraph("Effectivity: %.2f%%" %(100*allTime/self.nights),center_style)
                elements.append(p)
            
            elements.append(Spacer(1, 0.2 * inch))
            elements.append(PageBreak())
            
            
            #plots
            fig, axs = plt.subplots(2,2,dpi=300,figsize=(10,9))
            fig.text(0.5, 0.92, 'Observing nights', ha='center', fontsize=14, fontweight='bold')
            axs[0,0].pie(sups10.values(),labels=sups10.keys(),rotatelabels=True, textprops={'fontsize': 8})
            axs[0,1].pie(objs10.values(),labels=objs10.keys(),rotatelabels=True, textprops={'fontsize': 8})
            
            fig.text(0.5, 0.48, 'Observing time', ha='center', fontsize=14, fontweight='bold')
            axs[1,0].pie(supsT10.values(),labels=supsT10.keys(),rotatelabels=True,autopct='%d%%', textprops={'fontsize': 8})
            axs[1,1].pie(objsT10.values(),labels=objsT10.keys(),rotatelabels=True,autopct='%d%%', textprops={'fontsize': 8})
            
            elements.append(fig2image(fig))
            elements.append(Spacer(1, 0.2 * inch))
            elements.append(PageBreak())
            
            fig, axs = plt.subplots(2,1,dpi=300,figsize=(10,9))
            fig.text(0.5, 0.92, 'Observing nights', ha='center', fontsize=14, fontweight='bold')
            axs[0].pie(prog.values(),labels=prog.keys(),rotatelabels=True, textprops={'fontsize': 8})
            
            fig.text(0.5, 0.48, 'Observing time', ha='center', fontsize=14, fontweight='bold')
            axs[1].pie(progT.values(),labels=progT.keys(),rotatelabels=True,autopct='%d%%', textprops={'fontsize': 8})
            
            elements.append(fig2image(fig))
            elements.append(Spacer(1, 0.2 * inch))
            elements.append(PageBreak())
            
        else:
            #no data
            styles = getSampleStyleSheet()
            title_style = styles['Heading3']
            title_style.alignment = 1
            title_style.textColor='red'

            p = Paragraph("NO observations!",title_style)
            elements.append(p)
            elements.append(Spacer(1,0.2*inch))
            elements.append(Spacer(1, 0.2 * inch))
            elements.append(PageBreak())


        self.docElements.extend(elements)

        return elements;

    def write_pdfpage(self):
        self.doc.build(self.docElements, onFirstPage=first_page_layout, onLaterPages=page_layout)


# define layout for first page
def first_page_layout(canvas, doc):
    canvas.saveState()
    canvas.setFont('Times-Bold', 16)
    canvas.drawCentredString(PDFReport.PAGE_WIDTH / 2.0, PDFReport.PAGE_HEIGHT-0.5 * inch, 'Observing statistics')
    #add logo
    logo = ImageReader('logo.png')
    canvas.drawImage(logo, 10*mm, PDFReport.PAGE_HEIGHT-20*mm,width=50*mm,height=10*mm, mask='auto',preserveAspectRatio=True)

    canvas.setFont('Times-Bold', 14)
    canvas.drawCentredString(PDFReport.PAGE_WIDTH / 2.0, PDFReport.PAGE_HEIGHT-0.75 * inch, 'Observing period: '+doc.title)
    canvas.setFont('Times-Roman', 9)
    canvas.drawString(inch * 3, 0.75 * inch,
                      "Page: %d     Generated: %s     " % (
                      doc.page, PDFReport.timestamp))
    canvas.restoreState()

# define layout for subsequent pages
def later_page_layout(canvas, doc):
    canvas.saveState()
    canvas.setFont('Times-Roman', 9)
    canvas.drawString(inch, 0.75 * inch, "Page %d %s" % (doc.page, PDFReport.pageinfo))
    canvas.restoreState()

# define layout for subsequent pages
def page_layout(canvas, doc):
    canvas.saveState()
    canvas.setFont('Times-Roman', 9)
    canvas.drawString(inch * 3, 0.75 * inch,
                      "Page: %d     Generated: %s     " % (
                      doc.page, PDFReport.timestamp))
    canvas.restoreState()
