#!/usr/bin/python
import os
import sys
import tarfile

""" Class to backup a directory on a given template
    basicly it works for directories under /home dir
"""

class dbbackup():
    """ Constructor requires those parameters
        backup_dst : path where the archive will be created
        template_filename : Template file to choose backup folders
        chdir_path   : somewhere different form home
    """
    def __init__(self, backup_dst, template_filename=None, chdir_path=None,compression=None):
        self.chdir_path = chdir_path
        self.template_filename = template_filename
        self.tar= None
        self.backup_paths = None
        self.backup_dst= backup_dst
        self.tar_file = os.path.join(self.backup_dst,self.tar_filename)

        if self.compression:
            self.tar_opt ="w:gz"
            self.tar_filename= backup_dst + ".tar.gz"
        else:
            self.tar_opt = "w"
            self.tar_filename= backup_dst + ".tar"

        if template_filename:
            load_template()


    def __del__(self):
        """ Desctructor in case we forgot to close file
        """
        if self.tar:
            self.close_archive()

    def load_template(self, temp=None):
        """ Load the template file and populare backup_paths
        """
        if temp:
            self.tempalte_filename= temp
        try:
            temp_file = open(self.template_filename,'r')
            self.backup_paths = temp_file.readlines()
        except Exception,e:
            print "%s template dosyasi acilamadi hata: %s" % (self.template_filename,e)
        return self.backup_paths

    def open_archive(self):
        """ Try to open the tar archive to a destination
        """
        try:
            self.tar = tarfile.open(self.tar_file,self.tar_opt)
        except Exception,e:
            print "Unable to open %s error: %s" % (backup_dst, e)
        return self.tar

    def close_archive(self):
        try:
            self.tar.close()
            self.tar = None
        except Exception,e:
            print "Unable to close %s error: %s" % (backup_dst, e)
        return



    def backup(self,debug=False):
        """ This does the backup
            enable debug to see some fancy action
        """

        if not self.backup_paths:
            print "No information to backup. Does the template loaded?"
            return 0

        if self.chdir:
            if os.path.isdir(self.chdir_path):
                os.chdir(self.chdir_path)
            else:
                print "Unable to chdir to %s. No source directory found" %s (self.chdir_path)

        for path in self.backup_paths:
            path = path.rstrip()

            if os.path.exists(path):
                if debug: print "Backing up:",path
                try:
                    st = self.tar.add(path)
                except Exception,e:
                    print "%s dosyasinda hata:%s" %(path,e)
            else :
                print "Warning: backup path %s not found" % (path)

        return st


def main():
    pass

# Standard boilerplate to call the main() function to begin
# the program.
if __name__ == '__main__':
    main()
