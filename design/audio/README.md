# Arcade Sound Candidates

เสียง arcade ที่อนุมัติแล้วสำหรับ Photo Prompt เปิด `preview.html` ด้วย
Google Chrome แล้วกดฟังเสียงแต่ละรายการได้ทันที

| File | Intended cue | Duration |
| --- | --- | ---: |
| `ui-click.wav` | กดปุ่มหรือเลือกช่วงชั้น | 0.16 s |
| `prompt-submit.wav` | ส่งคำสั่งและเริ่มสร้างภาพ | 0.48 s |
| `countdown-tick.wav` | แต่ละวินาทีในช่วงนับถอยหลัง 5 วินาทีสุดท้าย | 0.14 s |
| `generation-complete.wav` | ภาพสร้างเสร็จ | 0.82 s |
| `score-reveal.wav` | แสดงคะแนนหรืออันดับ | 1.10 s |
| `generation-error.wav` | การสร้างภาพไม่สำเร็จ | 0.62 s |

ทุกไฟล์เป็น WAV mono, 44.1 kHz, 16-bit และจำกัด peak ไว้ที่ประมาณ
−9.9 dBFS เพื่อให้มี headroom ก่อนกำหนดระดับเสียงจริงในเกม

รันคำสั่งต่อไปนี้เพื่อสร้างไฟล์ใหม่จาก source เดิมแบบ deterministic:

```bash
uv run python design/audio/generate.py
```

`deno task app:build` คัดลอกเฉพาะ WAV ที่อนุมัติไป `dist/audio/` ระบบเกม
ใช้เสียงเหล่านี้เป็น progressive enhancement; หาก browser เล่นเสียงไม่ได้
เกมยังต้องทำงานต่อได้ตามปกติ
