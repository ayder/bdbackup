#!/usr/bin/python
from bdbackup import filebackup

b = backup('shopbg88', ".", "template.txt")
b.read_template()
b.open_archive()
b.do_backup(debug=True)
b.close_archive()
