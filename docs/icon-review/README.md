# App icon

The icon is a Canva export, kept as
[a source PDF](../../packaging/flatpak/icons/source/ModSync.pdf). The SVG
wraps that artwork in a rounded clipping path with a 96 pixel radius at 512
pixels. The PNG is rendered from the SVG with librsvg. Nothing in the supplied
design is redrawn, recolored, scaled, bordered or shadowed.

The comparison below shows the square export on the left and the rounded
version on the right, at 220, 128, 64 and 32 pixels on light and dark
backgrounds.

![Square export and rounded version on light and dark backgrounds](comparison.png)

Removing the clip and wrapper from the SVG reproduces the PDF-converted SVG
apart from its 512 pixel canvas. PNG pixels outside the corner regions match
the square export exactly.

The Flatpak installs only the PNG. Qt's SVG renderer ignores clipping paths,
so installing the SVG into the icon theme would show square corners. The SVG
stays in the repository as the editable source.
