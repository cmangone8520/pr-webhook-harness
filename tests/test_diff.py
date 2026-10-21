from prharness.review.diff import commentable_lines, truncate_diff

DIFF = """\
diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
-x = 1
+x = 2
+y = 3
 print(x)
@@ -10,2 +11,2 @@ def f():
-    return 1
+    return 2
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
\\ No newline at end of file
"""


def test_commentable_lines() -> None:
    assert commentable_lines(DIFF) == {"app.py": {1, 2, 3, 4, 11}, "new.py": {1, 2}}


def test_truncate_on_line_boundary() -> None:
    text, truncated = truncate_diff("aaa\nbbb\nccc", 9)
    assert (text, truncated) == ("aaa\nbbb", True)
    assert truncate_diff("short", 100) == ("short", False)
