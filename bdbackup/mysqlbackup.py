import gzip
import subprocess
import ConfigParser
import logging
import logging.config
from dboperations import mysqlops

class mysqlbackup():

    def __init__(self,configfile):
        self.configfile= configfile
        self.config = None
        init_logging()
        getconfig()


    def backup(self):
        mydb = mysqlops.pass
        pass

    def init_logging(self):
        logger = logging.getlogger('Backup_logger')
        logger.setLevel(logging.DEBUG)
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        logger.addHandler(ch)
        self.logger = logger

    def init_logging_from_config(self, configfile):
        logging.config.fileConfig('logging.conf')
        logger = logging.getLogger('backup_logger')


    def getconfig(self):
        config = ConfigParser.RawConfigParser()
        try:
            self.config = config.read(self.configfile)
        except Exception,e:
            logging.error ("Unable to read %s" % self.configfile)


    def mysqlbackup(self,domaindb):
        out_filename = "deneme.sql.gz"
        db_user= "root"
        db_pass= ""
        options = "--singe-transaction --databases"

        cmdL1 = ["mysqldump", "--user=" + db_user, "--password=" + db_pass, options, domaindb]
        p1 = subprocess.Popen(cmdL1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        dump_output = p1.communicate()[0]

        f = gzip.open(out_filename, "wb")
        f.write(dump_output)
        f.close()
