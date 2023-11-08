from PyPDF2 import PdfReader, PdfWriter
from pdf2docx import Converter
import os

# Open the large PDF file
pdf_file = './data/DAMA-DMBOK2.pdf'
pdf_reader = PdfReader(pdf_file)

# Get the number of pages
num_pages = len(pdf_reader.pages)

# Loop through each page and create a new PDF file
for i in range(num_pages):
    # Create a PDF writer object
    pdf_writer = PdfWriter()

    # Get the current page
    page = pdf_reader.pages[i]

    # Add the page to the writer
    pdf_writer.add_page(page)

    # Create a new PDF file name with the page number
    new_pdf_file = f'./data/split/DAMA-DMBOK2-page-{i+1}.pdf'

    # Ensure the directory exists before writing the file
    os.makedirs(os.path.dirname(new_pdf_file), exist_ok=True)

    # Write the page to the new PDF file
    with open(new_pdf_file, 'wb') as f:
        pdf_writer.write(f)

    # Create a Docx file name with the page number
    docx_file = f'./data/docx/DMBOK2-page-{i+1}.docx'

    # Ensure the directory exists before writing the file
    os.makedirs(os.path.dirname(docx_file), exist_ok=True)

    # Convert the new PDF file to a Docx file
    cv = Converter(new_pdf_file)
    cv.convert(docx_file, start=0, end=None)
    cv.close()
