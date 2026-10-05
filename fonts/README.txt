Put your own font files (.ttf / .otf) in this folder, or use the "+ Font" button in the editor.

Fonts here appear under "Your fonts" in the Font menu. When you edit existing text with
Font = Automatic, the editor uses:
  1. the PDF's own embedded font, if it still has every letter you typed;
  2. otherwise the same font from this folder or installed on the computer
     (for example DIN Next LT Arabic Bold, if you add it here);
  3. otherwise the closest free look-alike, keeping bold and italic:
       Helvetica            -> TeX Gyre Heros
       Helvetica Condensed,
       Arial Narrow         -> TeX Gyre Heros Cn
       Arial                -> Arial, else Liberation Sans (same letter widths)
       Times / Times New Roman -> Times New Roman, else Liberation Serif
       Courier / Courier New   -> Courier New, else Liberation Mono
       DIN Next             -> Tajawal (Arabic) / Barlow (Latin)
       Calibri, Verdana, Roboto ... -> Arial / Liberation Sans
       Georgia, Garamond ...        -> Times New Roman / Liberation Serif

Bundled fonts and their licences:
  Liberation Sans / Serif / Mono 2.1.5   SIL Open Font License   OFL-liberation.txt
  TeX Gyre Heros (and Heros Cn)          GUST Font License       GUST-license-texgyre.txt
  Tajawal                                SIL Open Font License   OFL-tajawal.txt
  Noto Kufi Arabic                       SIL Open Font License   OFL-notokufiarabic.txt
  Barlow                                 SIL Open Font License   OFL-barlow.txt
