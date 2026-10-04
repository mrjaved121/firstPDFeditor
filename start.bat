@echo off
cd /d "%~dp0"
python -c "import pymupdf, flask, PIL, pdf2docx, docx, pptx, openpyxl, anthropic" 2>nul || python -m pip install -r requirements.txt
python app.py
pause
