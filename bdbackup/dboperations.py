#!/usr/bin/python

import MySQLdb

# Open database connection
class mysqlops():

    def __init__(self):

        try:
            self.db = MySQLdb.connect("localhost","root","","backup_information" )
            self.cursor = db.cursor()
        except:
            print "Unable to connect mysql"
        self.dblist = []

    def get_dblist():
        # prepare a cursor object using cursor() method
        #cursor = db.cursor()

        # Prepare SQL query to INSERT a record into the database.
        sql = "select `ID` ,`DATABASE`, `LAST_BACKUP_TIMESTAMP` \
                from tbl_backup WHERE `STATUS` = 1"
        count = 0

        try:
           # Execute the SQL command
           self.cursor.execute(sql)
           # Fetch all the rows in a list of lists.
           results = self.cursor.fetchall()
           for row in results:
              print row
              id = row[0]
              dbname = row[1]
              backupts = row[2]
              self.dblist[count] = dbname
              count += 1
        except Exception, e:
           print "Error: unable to fecth data %s" % e
              # Now print fetched result

        print "ID=%d,DB=%s,TIME=%s" % \
                     (id, dbname, backupts )
        return self.dblist

        # disconnect from server

    def updatedb(dbname):
        """ Prepare SQL query to update required records"""
        sql = "UPDATE tbl_backup SET `LAST_BACKUP_TIMESTAMP`= now() WHERE `DATABASE` = '%d'" % (dbname)
        try:
            # Execute the SQL command
            self.cursor.execute(sql)
            # Commit your changes in the database
            self.db.commit()
        except:
            # Rollback in case there is any error
            self.db.rollback()

    def __del__(self):
        self.db.close()
