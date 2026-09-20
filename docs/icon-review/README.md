# Icon presentation review

Use the updated Canva export with more space around the letter and arrows,
preserved in [the source PDF](../../packaging/flatpak/icons/source/ModSync.pdf).
Apply transparent rounded corners with a 96px radius at 512px (up from 64px
in the initial proposal). The extra inset leaves room for the deeper curve.

The SVG wraps the supplied artwork in a rounded clipping path; the PNG is
rendered from that SVG using librsvg. No redrawing, recoloring, extra scaling,
border or shadow is applied to the supplied design.

The comparison shows the new square Canva export on the left and its rounded
version on the right, at 220, 128, 64 and 32 pixels on each background.

![Updated export and rounded version on light and dark backgrounds](comparison.png)

Validation: removing the new clip and wrapper reproduces the PDF-converted SVG
apart from its 512px canvas dimensions. PNG pixels outside the corner regions
match the square export exactly. The Flatpak installs only the PNG; the SVG remains the editable source.
Qt ignores SVG clipping paths, so exporting the SVG to the icon theme would
lose the rounded corners.
