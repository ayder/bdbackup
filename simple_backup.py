#!/usr/bin/python
from bdbackup import bdbackup

b = bdbackup(backup_dst='/home/sinan/bdbackup', template_filename="template.txt",chdir="/home/sinan")
b.backup(debug=True)
b.close_archive()
