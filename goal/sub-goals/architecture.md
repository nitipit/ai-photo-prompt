# Architecture

โครงสร้างเทคนิคควรสนับสนุนเกมบูธที่เรียบง่ายและดูแลต่อได้: FastAPI และ
Jinja2/HTML-first เป็นแกนของ multi-page webapp, Swup ใช้เปลี่ยน scene อย่าง
ลื่นไหลโดยไม่เป็นเจ้าของ game state, ShelfDB ใช้เก็บข้อมูล, Dictify ใช้กำหนด
และตรวจสอบ schema กับ object, และ Adapter Web Components ใช้สร้าง UI พร้อม
Arrow JS สำหรับพฤติกรรม reactive ภายใน component โดยทำงานร่วมกับ Deno และ
esbuild ตาม pattern ของ frontend system ที่ตกลงกันไว้ โครงสร้างไม่ควรเป็น SPA
หรือแยก frontend ออกจาก webroot source/build mirror ที่ตกลงกันไว้
