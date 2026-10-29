from pymongo import MongoClient, version


class DbConnector:
    """
    Connects to the MongoDB server on the Ubuntu virtual machine.
    Connector needs HOST, USER and PASSWORD to connect.

    Example:
    HOST = "tdt4225-00.idi.ntnu.no" // Your server IP address/domain name
    USER = "testuser" // This is the user you created and added privileges for
    PASSWORD = "test123" // The password you set for said user
    """

    def __init__(self,
                 DATABASE='movies_assignment',
                 HOST="localhost",
                 USER="",
                 PASSWORD=""):
        uri = "mongodb://%s:%s@%s/%s" % (USER, PASSWORD, HOST, DATABASE)
        if not USER:
            uri = "mongodb://%s/%s" % (HOST, DATABASE)
        # Connect to the databases
        try:
            self.client = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
            self.client.admin.command("ping")
            self.db = self.client[DATABASE]
        except Exception as e:
            print("ERROR: Failed to connect to db:", e)
            if hasattr(self, "client"):
                self.client.close()
            raise

        #get database information
        print("You are connected to the database:", self.db.name)
        print("-----------------------------------------------\n")

    def close_connection(self):
        #close the cursor
        #close the DB connection
        self.client.close()
        print("\n-----------------------------------------------")
        print("Connection to %s-db is closed" % self.db.name)
