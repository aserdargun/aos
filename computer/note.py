import json
import os
from pathlib import Path
import tkinter as tk


os.umask(0o077)
window = tk.Tk()
window.title('AOS Synthetic Input')
window.geometry('520x160+100+100')
tk.Label(window, text='Synthetic input acceptance — no external submission').pack(pady=20)
text = tk.StringVar()
entry = tk.Entry(window, textvariable=text, width=55)
entry.pack()
entry.focus_set()


def changed(*arguments):
    Path('/tmp/aos-note.json').write_text(json.dumps({'text': text.get()}))


text.trace_add('write', changed)
changed()
window.mainloop()
