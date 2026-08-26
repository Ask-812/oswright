---
name: Bug report
about: Something does not work
labels: bug
---

**What happened, and what you expected instead**

**Which tool call, with its arguments**

```
click_element(text="...", window_title="...")
```

**What the tool returned**

Paste the JSON. If it reports a `rung` and `rungs_tried`, keep them -- they say
which perception path answered, which is usually the whole diagnosis.

**Environment**

- oswright version:
- Windows version and display scaling:
- Application being driven:

**If it is a perception problem**

Whether the text is visible on screen, and whether these find it:

```
find_element(text="...")            # the cascade
read_screen()                        # everything OCR sees
list_ui_elements(window_title="...") # what the accessibility tree exposes
```

Text that OCR reads differently from how it is written is a known class of
problem -- underscores in particular come back spaced. `docs/ENGINEERING_LOG.md`
section 2.18 has the details.
