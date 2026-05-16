from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from datetime import datetime
import os
import re


def safe_filename(text):
    text = text or "quote"
    text = re.sub(r"[^a-zA-Z0-9_-]+", "_", text)
    return text[:50]


def generate_quote_pdf(result, contractor, customer_name, job_description):
    business_name = contractor.get("Business Name", "Your Contractor")
    business_phone = contractor.get("Business Phone", contractor.get("Notify SMS", ""))
    business_email = contractor.get("Notify Email", "")
    address = result.get("address", "")
    quote_range = result.get("quote_range", "TBD")
    square_footage = result.get("square_footage", 0)
    analysis = result.get("analysis", "")
    satellite_url = result.get("satellite_url", "")

    os.makedirs("/tmp/quotes", exist_ok=True)

    filename = f"{safe_filename(customer_name)}_{safe_filename(address)}_quote.pdf"
    pdf_path = os.path.join("/tmp/quotes", filename)

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=letter,
        rightMargin=0.6 * inch,
        leftMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        fontSize=22,
        textColor=colors.HexColor("#111111"),
        spaceAfter=6,
    )

    brand_style = ParagraphStyle(
        "BrandStyle",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#22c55e"),
        spaceAfter=18,
    )

    heading_style = ParagraphStyle(
        "HeadingStyle",
        parent=styles["Heading2"],
        fontSize=14,
        textColor=colors.HexColor("#111111"),
        spaceBefore=12,
        spaceAfter=8,
    )

    normal_style = ParagraphStyle(
        "NormalStyle",
        parent=styles["Normal"],
        fontSize=10.5,
        leading=15,
    )

    small_style = ParagraphStyle(
        "SmallStyle",
        parent=styles["Normal"],
        fontSize=8.5,
        textColor=colors.gray,
        leading=12,
    )

    story = []

    story.append(Paragraph(business_name, title_style))
    story.append(Paragraph("Powered by CrewCachePro", brand_style))

    story.append(Paragraph("Preliminary Property Estimate", heading_style))

    summary_data = [
        ["Customer", customer_name or "Customer"],
        ["Property Address", address],
        ["Service Requested", job_description or "Service"],
        ["Estimated Work Area", f"{int(square_footage):,} sq ft" if square_footage else "Verify on-site"],
        ["Estimated Investment", quote_range],
        ["Date Prepared", datetime.now().strftime("%B %d, %Y")],
    ]

    table = Table(summary_data, colWidths=[1.8 * inch, 4.7 * inch])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#111111")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(table)

    story.append(Spacer(1, 14))

    story.append(Paragraph("Scope of Work", heading_style))
    story.append(Paragraph(
        f"This is a preliminary estimate for {job_description or 'the requested service'} "
        f"at {address}. Final pricing may be adjusted after an on-site review.",
        normal_style
    ))

    story.append(Paragraph("Aerial Review Notes", heading_style))
    clean_analysis = (analysis or "No aerial analysis available.").replace("\n", "<br/>")
    story.append(Paragraph(clean_analysis, normal_style))

    if satellite_url:
        story.append(Paragraph("Satellite Image Link", heading_style))
        story.append(Paragraph(satellite_url, small_style))

    story.append(Paragraph("Important Notes", heading_style))
    story.append(Paragraph(
        "This estimate is based on aerial review and available property information. "
        "Measurements are approximate. Final pricing should be confirmed before work begins.",
        small_style
    ))

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        f"{business_name}<br/>{business_phone}<br/>{business_email}",
        small_style
    ))

    doc.build(story)

    return pdf_path
