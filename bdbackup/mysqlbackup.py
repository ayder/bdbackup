import gzip
import subprocess

out_filename = "deneme.sql.gz"
db_user= "root"
db_pass= ""
domaindb = "test"
options = "--singe-transaction --databases"

cmdL1 = ["mysqldump", "--user=" + db_user, "--password=" + db_pass, options, domaindb]
p1 = subprocess.Popen(cmdL1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
dump_output = p1.communicate()[0]

f = gzip.open(out_filename, "wb")
f.write(dump_output)
f.close()
