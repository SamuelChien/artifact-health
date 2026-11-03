"""Generate explicitly disclosed reconstructed analyses, never historical records."""
import json
from pathlib import Path

from .contracts import contained, digest, resolve, task_identity, require
from .native import extract
from .store import write_json


def create_output(store, task, solution, execution):
    identity=task_identity(task); directory=store.run/"generated"/identity
    directory.mkdir(parents=True,exist_ok=True)
    inputs=store.units([r["unit_id"] for r in task["inputs"]])
    for block in solution["sections"]+solution["tables"]:
        for ref in block["evidence"]:
            require(contained(ref,task["inputs"],inputs), "generated analysis cited unselected input content")
    for table in solution["tables"]:
        require(table["columns"] and all(len(r)==len(table["columns"]) for r in table["rows"]), "generated table is not rectangular")
    disclosure="AI-generated reconstructed analysis. This is not an original employee document or proof that a business action occurred."
    document={"disclosure":disclosure,**solution}
    kind=task.get("output_format","json"); path=directory/("analysis."+{"text":"txt"}.get(kind,kind))
    if kind=="xlsx":
        from openpyxl import Workbook
        workbook=Workbook(); notes=workbook.active; notes.title="Analysis"
        notes.append([solution["title"]]); notes.append([disclosure])
        for section in solution["sections"]: notes.append([section["heading"],section["text"]])
        for n,table in enumerate(solution["tables"],1):
            sheet=workbook.create_sheet("Table "+str(n)); sheet.append(table["columns"])
            for row in table["rows"]:
                sheet.append(["'"+v if v.lstrip().startswith(("=","+","-","@")) else v for v in row])
        workbook.properties.creator="Codex; reconstructed analysis"; workbook.save(path)
    elif kind=="docx":
        from docx import Document
        doc=Document(); doc.add_heading(solution["title"],0); doc.add_paragraph(disclosure)
        for section in solution["sections"]: doc.add_heading(section["heading"],1); doc.add_paragraph(section["text"])
        for table in solution["tables"]:
            doc.add_heading(table["title"],1)
            grid=doc.add_table(rows=1,cols=max(1,len(table["columns"]))); grid.style="Table Grid"
            for cell,value in zip(grid.rows[0].cells,table["columns"]): cell.text=value
            for row in table["rows"]:
                for cell,value in zip(grid.add_row().cells,row): cell.text=value
        doc.core_properties.author="Codex; reconstructed analysis"; doc.save(path)
    elif kind=="pdf":
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib import colors
        from xml.sax.saxutils import escape
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        styles=getSampleStyleSheet()
        if any('\u3400'<=char<='\u9fff' for char in json.dumps(solution,ensure_ascii=False)):
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            for style in styles.byName.values(): style.fontName="STSong-Light"
        story=[Paragraph(escape(solution["title"]),styles["Title"]),Paragraph(disclosure,styles["BodyText"])]
        for section in solution["sections"]:
            story += [Spacer(1,12),Paragraph(escape(section["heading"]),styles["Heading2"]),Paragraph(escape(section["text"]).replace("\n","<br/>"),styles["BodyText"])]
        for table in solution["tables"]:
            rows=[[Paragraph(escape(v),styles["BodyText"]) for v in row] for row in [table["columns"]]+table["rows"]]
            if rows:
                grid=LongTable(rows,repeatRows=1,colWidths=[470/max(1,len(table["columns"]))]*max(1,len(table["columns"])))
                grid.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.4,colors.grey),("VALIGN",(0,0),(-1,-1),"TOP")]))
                story += [Spacer(1,12),grid]
        SimpleDocTemplate(str(path),title=solution["title"],author="Codex; reconstructed analysis").build(story)
    elif kind=="text":
        path.write_text(disclosure+"\n\n"+json.dumps(solution,ensure_ascii=False,indent=2))
    else:
        write_json(path,document)
    sha,blob,_=store.snapshot(path); artifact_id=digest([sha,path.suffix.lower()])
    config=store.get("config")
    data=extract(blob,artifact_id,directory/"review",{"render":True,"tessdata":config.get("tessdata"),"ocr_languages":config.get("ocr_languages","eng")})
    data.update(id=artifact_id,sha256=sha,extension=path.suffix.lower(),blob=str(blob),paths=[str(path.relative_to(store.run))],
                generated=True,provenance={"business_origin":"reconstructed_analysis","ai_involvement":"declared_generated",
                                           "generation_execution":execution,"input_units":task["inputs"]})
    store.add_artifact(artifact_id,sha,path.suffix.lower(),data)
    output_refs=[{"unit_id":u["id"],"start":0,"end":len(u["text"])} for u in data["units"]]
    task={**task,"outputs":output_refs,"generation_execution_id":execution["execution_id"]}
    # Preserve the observed source workflow; add the newly executed analytical output explicitly.
    stages=task.get("stages",[])
    if not stages:
        stages=[{"id":"source","name":"source","evidence":task["inputs"]}]
    stages=[n for n in stages if n.get("evidence")]
    existing={n["id"] for n in stages}; output_id='generated-output'
    while output_id in existing:output_id+='-new'
    stages.append({"id":output_id,"name":"output","evidence":output_refs})
    edges=[e for e in task.get("edges",[]) if e["from"] in existing and e["to"] in existing]
    terminal=existing-{e["from"] for e in edges}
    for source in sorted(terminal):
        edges.append({"from":source,"to":output_id,"basis":"Recorded generation execution over the selected inputs", "evidence":task["inputs"]+output_refs})
    task.update(stages=stages,edges=edges,required_stages=sorted(set(task.get('required_stages',[]))|{n["name"] for n in stages}),
                generated_disclosure=disclosure)
    write_json(directory/"generation.json",{"task_id":identity,"source_inputs":task["inputs"],"output_sha256":sha,"execution":execution})
    return task
