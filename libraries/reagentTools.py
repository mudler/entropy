#!/usr/bin/python
'''
    # DESCRIPTION:
    # generic tools for reagent application

    Copyright (C) 2007 Fabio Erculiani

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation; either version 2 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program; if not, write to the Free Software
    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
'''

# Never do "import portage" here, please use entropyTools binding

# EXIT STATUSES: 500-599

from entropyConstants import *
from serverConstants import *
from entropyTools import *
import commands
import re
import sys
import os
import string
from portageTools import synthetizeRoughDependencies, getThirdPartyMirrors, getPackagesInSystem, getConfigProtectAndMask

# Logging initialization
import logTools
reagentLog = logTools.LogFile(level=etpConst['reagentloglevel'],filename = etpConst['reagentlogfile'], header = "[Reagent]")

# reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"testFunction: example. ")

def generator(package, enzymeRequestBump = False, dbconnection = None, enzymeRequestBranch = "unstable"):

    reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"generator: called -> Package: "+str(package)+" | enzymeRequestBump: "+str(enzymeRequestBump)+" | dbconnection: "+str(dbconnection))

    # check if the package provided is valid
    validFile = False
    if os.path.isfile(package) and package.endswith(".tbz2"):
	validFile = True
    if (not validFile):
	print_warning(package+" does not exist !")

    packagename = os.path.basename(package)

    print_info(yellow(" * ")+red("Processing: ")+bold(packagename)+red(", please wait..."))
    etpData = extractPkgData(package,enzymeRequestBranch)
    
    
    if dbconnection is None:
	dbconn = databaseTools.etpDatabase(readOnly = False, noUpload = True)
    else:
	dbconn = dbconnection

    idpk, revision, etpDataUpdated, accepted = dbconn.handlePackage(etpData,enzymeRequestBump)
    
    # add package info to our official repository etpConst['officialrepositoryname']
    if (accepted):
        dbconn.removePackageFromInstalledTable(idpk)
	dbconn.addPackageToInstalledTable(idpk,etpConst['officialrepositoryname'])
    
    # return back also the new possible package filename, so that we can make decisions on that
    newFileName = os.path.basename(etpDataUpdated['download'])
    
    if dbconnection is None:
	dbconn.commitChanges()
	dbconn.closeDB()

    if (accepted) and (revision != 0):
	reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"generator: entry for "+str(packagename)+" has been updated to revision: "+str(revision))
	print_info(green(" * ")+red("Package ")+bold(packagename)+red(" entry has been updated. Revision: ")+bold(str(revision)))
	return True, newFileName, idpk
    elif (accepted) and (revision == 0):
	reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"generator: entry for "+str(packagename)+" newly created.")
	print_info(green(" * ")+red("Package ")+bold(packagename)+red(" entry newly created."))
	return True, newFileName, idpk
    else:
	reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"generator: entry for "+str(packagename)+" kept intact, no updates needed.")
	print_info(green(" * ")+red("Package ")+bold(packagename)+red(" does not need to be updated. Current revision: ")+bold(str(revision)))
	return False, newFileName, idpk


# This tool is used by Entropy after enzyme, it simply parses the content of etpConst['packagesstoredir']
def enzyme(options):

    reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"enzyme: called -> options: "+str(options))

    enzymeRequestBump = False
    enzymeRequestBranch = "unstable"
    #_atoms = []
    for i in options:
        if ( i == "--force-bump" ):
	    enzymeRequestBump = True
        if ( i == "--branch=" and len(i.split("=")) == 2 ):
	    mybranch = i.split("=")[1]
	    if (mybranch):
	        enzymeRequestBranch = mybranch

    tbz2files = os.listdir(etpConst['packagesstoredir'])
    totalCounter = 0
    # counting the number of files
    for i in tbz2files:
	totalCounter += 1

    if (totalCounter == 0):
	print_info(yellow(" * ")+red("Nothing to do, check later."))
	# then exit gracefully
	sys.exit(0)

    # open db connection
    dbconn = databaseTools.etpDatabase(readOnly = False, noUpload = True)

    counter = 0
    etpCreated = 0
    etpNotCreated = 0
    for tbz2 in tbz2files:
	counter += 1
	tbz2name = tbz2.split("/")[len(tbz2.split("/"))-1]
	print_info(" ("+str(counter)+"/"+str(totalCounter)+") Processing "+tbz2name)
	tbz2path = etpConst['packagesstoredir']+"/"+tbz2
	rc, newFileName, idpk = generator(tbz2path, enzymeRequestBump, dbconn, enzymeRequestBranch)
	if (rc):
	    etpCreated += 1
	    # move the file with its new name
	    spawnCommand("mv "+tbz2path+" "+etpConst['packagessuploaddir']+"/"+newFileName+" -f")
	    
	    print_info(yellow(" * ")+red("Injecting database information into ")+bold(newFileName)+red(", please wait..."), back = True)
	    # uncompressing
	    tdir = etpConst['packagestmpdir']+"/injection"
	    if os.path.isdir(tdir):
	        os.system("rm -rf "+tdir)
	    os.makedirs(tdir)
	    os.mkdir(tdir+etpConst['packagecontentdir']) # content directory
	    os.mkdir(tdir+etpConst['packagedbdir']) # content directory
	    dbpath = tdir+etpConst['packagedbdir']+etpConst['packagedbfile']
	    # fill /package
	    spawnCommand("mv "+etpConst['packagessuploaddir']+"/"+newFileName+" "+tdir+etpConst['packagecontentdir']+"/")
	    # create db
	    pkgDbconn = databaseTools.etpDatabase(readOnly = False, noUpload = True, dbFile = dbpath, clientDatabase = True)
	    pkgDbconn.initializeDatabase()
	    data = dbconn.getPackageData(idpk)
	    rev = dbconn.retrieveRevision(idpk)
	    # inject
	    pkgDbconn.addPackage(data, revision = rev, wantedBranch = data['branch'], addBranch = False)
	    pkgDbconn.closeDB()
	    # recompose the new file
	    compressTarBz2(etpConst['packagessuploaddir']+"/"+newFileName,tdir+"/")
	    # update the checksum in the database
	    digest = md5sum(etpConst['packagessuploaddir']+"/"+newFileName)
	    dbconn.setDigest(idpk,digest)
	    hashFilePath = createHashFile(etpConst['packagessuploaddir']+"/"+newFileName)
	    # remove tdir
	    spawnCommand("rm -rf "+tdir)
	    print_info(yellow(" * ")+red("Database injection complete for ")+newFileName)
	    
	else:
	    etpNotCreated += 1
	    spawnCommand("rm -rf "+tbz2path)
	dbconn.commitChanges()

    dbconn.commitChanges()
    dbconn.closeDB()

    print_info(green(" * ")+red("Statistics: ")+blue("Entries created/updated: ")+bold(str(etpCreated))+yellow(" - ")+darkblue("Entries discarded: ")+bold(str(etpNotCreated)))

# This function extracts all the info from a .tbz2 file and returns them
def extractPkgData(package, etpBranch = "unstable", structuredLayout = False):

    reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"extractPkgData: called -> package: "+str(package))

    # Clean the variables
    for i in etpData:
	etpData[i] = u""

    print_info(yellow(" * ")+red("Getting package name/version..."),back = True)
    tbz2File = package
    package = package.split(".tbz2")[0]
    if package.split("-")[len(package.split("-"))-1].startswith("unstable"):
        package = string.join(package.split("-unstable")[:len(package.split("-unstable"))-1],"-unstable")
    if package.split("-")[len(package.split("-"))-1].startswith("stable"):
	etpBranch = "stable"
        package = string.join(package.split("-stable")[:len(package.split("-stable"))-1],"-stable")
    if package.split("-")[len(package.split("-"))-1].startswith("t"):
        package = string.join(package.split("-t")[:len(package.split("-t"))-1],"-t")
    
    package = package.split("-")
    pkgname = ""
    pkglen = len(package)
    if package[pkglen-1].startswith("r"):
        pkgver = package[pkglen-2]+"-"+package[pkglen-1]
	pkglen -= 2
    else:
	pkgver = package[len(package)-1]
	pkglen -= 1
    for i in range(pkglen):
	if i == pkglen-1:
	    pkgname += package[i]
	else:
	    pkgname += package[i]+"-"
    pkgname = pkgname.split("/")[len(pkgname.split("/"))-1]

    # Fill Package name and version
    etpData['name'] = pkgname
    etpData['version'] = pkgver

    print_info(yellow(" * ")+red("Getting package md5..."),back = True)
    # .tbz2 md5
    etpData['digest'] = md5sum(tbz2File)

    if (structuredLayout):
	# extract tbz2
	structuredPackageDir = etpConst['packagestmpdir']+"/"+etpData['name']+"-"+etpData['version']+"-structured"
	if os.path.isdir(structuredPackageDir):
	    spawnCommand("rm -rf "+structuredPackageDir)
	os.makedirs(structuredPackageDir)
	uncompressTarBz2(tbz2File,structuredPackageDir)
	tbz2filename = os.path.basename(tbz2File)
	tbz2File = structuredPackageDir+etpConst['packagecontentdir']+"/"+tbz2filename

    print_info(yellow(" * ")+red("Getting package mtime..."),back = True)
    # .tbz2 md5
    etpData['datecreation'] = str(getFileUnixMtime(tbz2File))
    
    print_info(yellow(" * ")+red("Getting package size..."),back = True)
    # .tbz2 byte size
    etpData['size'] = str(os.stat(tbz2File)[6])
    
    print_info(yellow(" * ")+red("Unpacking package data..."),back = True)
    # unpack file
    tbz2TmpDir = etpConst['packagestmpdir']+"/"+etpData['name']+"-"+etpData['version']+"/"
    extractXpak(tbz2File,tbz2TmpDir)

    print_info(yellow(" * ")+red("Getting package CHOST..."),back = True)
    # Fill chost
    f = open(tbz2TmpDir+dbCHOST,"r")
    etpData['chost'] = f.readline().strip()
    f.close()

    print_info(yellow(" * ")+red("Setting package branch..."),back = True)
    # always unstable when created
    i = etpConst['branches'].index(etpBranch)
    etpData['branch'] = etpConst['branches'][i]

    print_info(yellow(" * ")+red("Getting package description..."),back = True)
    # Fill description
    etpData['description'] = ""
    try:
        f = open(tbz2TmpDir+dbDESCRIPTION,"r")
        etpData['description'] = f.readline().strip()
        f.close()
    except IOError:
        pass

    print_info(yellow(" * ")+red("Getting package homepage..."),back = True)
    # Fill homepage
    etpData['homepage'] = ""
    try:
        f = open(tbz2TmpDir+dbHOMEPAGE,"r")
        etpData['homepage'] = f.readline().strip()
        f.close()
    except IOError:
        pass

    print_info(yellow(" * ")+red("Getting package slot information..."),back = True)
    # fill slot, if it is
    etpData['slot'] = ""
    try:
        f = open(tbz2TmpDir+dbSLOT,"r")
        etpData['slot'] = f.readline().strip()
        f.close()
    except IOError:
        pass

    print_info(yellow(" * ")+red("Getting package content..."),back = True)
    # dbCONTENTS
    etpData['content'] = []
    try:
        f = open(tbz2TmpDir+dbCONTENTS,"r")
        content = f.readlines()
        f.close()
	outcontent = []
	for line in content:
	    line = line.strip().split()
	    if (line[0] == "obj") or (line[0] == "sym"):
		# remove first object (obj or sym)
		datafile = line[1:]
		# remove checksum and mtime - obj and sym have it
		try:
		    if line[0] == "obj":
		        datafile = datafile[:len(datafile)-2]
		    else:
			datafile = datafile[:len(datafile)-3]
		    datafile = string.join(datafile,' ')
		except:
		    datafile = datafile[0] # FIXME: handle shit better
		outcontent.append(datafile)
	# filter bad utf-8 chars
	_outcontent = []
	for i in outcontent:
	    try:
		i = i.encode(sys.getfilesystemencoding())
		_outcontent.append(i)
	    except:
		pass
	outcontent = _outcontent
	for i in outcontent:
	    etpData['content'].append(i.encode("utf-8"))
	
    except IOError:
        pass

    # files size on disk
    if (etpData['content']):
	etpData['disksize'] = 0
	for file in etpData['content']:
	    try:
		size = os.stat(file)[6]
		etpData['disksize'] += size
	    except:
		pass
    else:
	etpData['disksize'] = 0

    # [][][] Kernel dependent packages hook [][][]
    kernelDependentModule = False
    for file in etpData['content']:
	if file.find("/lib/modules/") != -1:
	    kernelDependentModule = True
	    # get the version of the modules
	    kmodver = file.split("/lib/modules/")[1]
	    kmodver = kmodver.split("/")[0]
	    # substitute "-" with "_"
	    kmodver = re.subn("-","_", kmodver)
	    if len(kmodver) >= 2:
		kmodver = kmodver[0]

	    lp = kmodver.split("-")[len(kmodver.split("-"))-1]
	    if lp.startswith("r"):
	        kname = kmodver.split("_")[len(kmodver.split("_"))-2]
	        kver = kmodver.split("_")[0]+"-"+kmodver.split("_")[len(kmodver.split("_"))-1]
	    else:
	        kname = kmodver.split("_")[len(kmodver.split("_"))-1]
	        kver = kmodver.split("_")[0]
	    break
    # validate the results above
    if (kernelDependentModule):
	matchatom = "linux-"+kname+"-"+kver
	if (matchatom == etpData['name']+"-"+etpData['version']):
	    # discard
	    kernelDependentModule = False

    # add strict kernel dependency
    # done below
    
    # modify etpData['download']
    # done below

    print_info(yellow(" * ")+red("Getting package download URL..."),back = True)
    # Fill download relative URI
    if (kernelDependentModule):
	etpData['versiontag'] = "t"+kmodver
	versiontag = "-"+etpData['versiontag']
    else:
	versiontag = ""
    etpData['download'] = etpConst['binaryurirelativepath']+etpData['name']+"-"+etpData['version']+versiontag+".tbz2"

    print_info(yellow(" * ")+red("Getting package category..."),back = True)
    # Fill category
    f = open(tbz2TmpDir+dbCATEGORY,"r")
    etpData['category'] = f.readline().strip()
    f.close()

    print_info(yellow(" * ")+red("Getting package CFLAGS..."),back = True)
    # Fill CFLAGS
    etpData['cflags'] = ""
    try:
        f = open(tbz2TmpDir+dbCFLAGS,"r")
        etpData['cflags'] = f.readline().strip()
        f.close()
    except IOError:
        pass

    print_info(yellow(" * ")+red("Getting package CXXFLAGS..."),back = True)
    # Fill CXXFLAGS
    etpData['cxxflags'] = ""
    try:
        f = open(tbz2TmpDir+dbCXXFLAGS,"r")
        etpData['cxxflags'] = f.readline().strip()
        f.close()
    except IOError:
        pass

    print_info(yellow(" * ")+red("Getting package License information..."),back = True)
    # Fill license
    etpData['license'] = []
    try:
        f = open(tbz2TmpDir+dbLICENSE,"r")
	# strip away || ( )
	tmpLic = f.readline().strip().split()
	f.close()
	for x in tmpLic:
	    if x:
		if (not x.startswith("|")) and (not x.startswith("(")) and (not x.startswith(")")):
		    etpData['license'].append(x)
	etpData['license'] = string.join(etpData['license']," ")
    except IOError:
	etpData['license'] = ""
        pass

    print_info(yellow(" * ")+red("Getting package USE flags..."),back = True)
    # Fill USE
    etpData['useflags'] = []
    f = open(tbz2TmpDir+dbUSE,"r")
    tmpUSE = f.readline().strip()
    f.close()

    try:
        f = open(tbz2TmpDir+dbIUSE,"r")
        tmpIUSE = f.readline().strip().split()
        f.close()
    except IOError:
        tmpIUSE = []

    PackageFlags = []
    for x in tmpUSE.split():
	if (x):
	    PackageFlags.append(x)

    for i in tmpIUSE:
	try:
	    PackageFlags.index(i)
	    etpData['useflags'].append(i)
	except:
	    etpData['useflags'].append("-"+i)

    print_info(yellow(" * ")+red("Getting package provide content..."),back = True)
    # Fill Provide
    etpData['provide'] = []
    try:
        f = open(tbz2TmpDir+dbPROVIDE,"r")
        provide = f.readline().strip()
        f.close()
	if (provide):
	    provide = provide.split()
	    for x in provide:
		etpData['provide'].append(x)
    except:
        pass

    print_info(yellow(" * ")+red("Getting package sources information..."),back = True)
    # Fill sources
    etpData['sources'] = []
    try:
        f = open(tbz2TmpDir+dbSRC_URI,"r")
	sources = f.readline().strip().split()
        f.close()
	tmpData = []
	cnt = -1
	skip = False
	etpData['sources'] = []
	
	for source in sources:
	    cnt += +1
	    if source.endswith("?"):
		# it's an use flag
		source = source[:len(source)-1]
		direction = True
		if source.startswith("!"):
		    direction = False
		    source = source[1:]
		# now get the useflag
		useflag = False
		try:
		    etpData['useflags'].index(source)
		    useflag = True
		except:
		    pass
		
		
		if (useflag) and (direction): # useflag is enabled and it's asking for sources or useflag is not enabled and it's not not (= True) asking for sources
		    # ack parsing from ( to )
		    skip = False
		elif (useflag) and (not direction):
		    # deny parsing from ( to )
		    skip = True
		elif (not useflag) and (direction):
		    # deny parsing from ( to )
		    skip = True
		else:
		    # ack parsing from ( to )
		    skip = False

	    elif source.startswith(")"):
		# reset skip
		skip = False

	    elif (not source.startswith("(")):
		if (not skip):
		    etpData['sources'].append(source)
    
    except IOError:
	pass

    print_info(yellow(" * ")+red("Getting package mirrors list..."),back = True)
    # manage etpData['sources'] to create etpData['mirrorlinks']
    # =mirror://openoffice|link1|link2|link3
    etpData['mirrorlinks'] = []
    for i in etpData['sources']:
        if i.startswith("mirror://"):
	    # parse what mirror I need
	    mirrorURI = i.split("/")[2]
	    mirrorlist = getThirdPartyMirrors(mirrorURI)
            etpData['mirrorlinks'].append([mirrorURI,mirrorlist]) # mirrorURI = openoffice and mirrorlist = [link1, link2, link3]

    print_info(yellow(" * ")+red("Getting source package supported ARCHs..."),back = True)
    # fill KEYWORDS
    etpData['keywords'] = []
    try:
        f = open(tbz2TmpDir+dbKEYWORDS,"r")
        cnt = f.readline().strip().split()
	for i in cnt:
	    if i:
		etpData['keywords'].append(i)
        f.close()
    except IOError:
	pass

    print_info(yellow(" * ")+red("Getting package supported ARCHs..."),back = True)
    
    # fill ARCHs
    kwords = etpData['keywords']
    _kwords = []
    for i in kwords:
	if i.startswith("~"):
	    i = i[1:]
	_kwords.append(i)
    etpData['binkeywords'] = []
    for i in etpConst['supportedarchs']:
	try:
	    x = _kwords.index(i)
	    etpData['binkeywords'].append(i)
	except:
	    pass

    print_info(yellow(" * ")+red("Getting package dependencies..."),back = True)
    # Fill dependencies
    # to fill dependencies we use *DEPEND files
    f = open(tbz2TmpDir+dbDEPEND,"r")
    roughDependencies = f.readline().strip()
    f.close()
    f = open(tbz2TmpDir+dbRDEPEND,"r")
    roughDependencies += " "+f.readline().strip()
    f.close()
    f = open(tbz2TmpDir+dbPDEPEND,"r")
    roughDependencies += " "+f.readline().strip()
    f.close()
    roughDependencies = roughDependencies.split()
    
    # variables filled
    # etpData['dependencies'], etpData['conflicts']
    deps,conflicts = synthetizeRoughDependencies(roughDependencies,string.join(PackageFlags," "))
    etpData['dependencies'] = []
    for i in deps.split():
	etpData['dependencies'].append(i)
    etpData['conflicts'] = []
    for i in conflicts.split():
	etpData['conflicts'].append(i)
    
    # filter development dependencies
    devPackage = False
    for cat in etpConst['developmentcategories']:
	if etpData['category'].startswith(cat):
	    devPackage = True
	    break
    if (not devPackage):
	# rip away building dependencies
	if (etpData['dependencies']):
	    newdeps = etpData['dependencies'][:]
	    for dep in newdeps:
		for develdep in etpConst['dependenciesfilter']:
		    if dep.find(develdep) != -1:
			try:
			    while 1: etpData['dependencies'].remove(dep)
			except:
			    pass
	
    
    if (kernelDependentModule):
	# add kname to the dependency
	etpData['dependencies'].append("sys-kernel/linux-"+kname+"-"+kver)

    print_info(yellow(" * ")+red("Getting System package List..."),back = True)
    # write only if it's a systempackage
    systemPackages = getPackagesInSystem()
    for x in systemPackages:
	x = dep_getkey(x)
	y = etpData['category']+"/"+etpData['name']
	if x == y:
	    # found
	    etpData['systempackage'] = "xxx"
	    break

    print_info(yellow(" * ")+red("Getting CONFIG_PROTECT/CONFIG_PROTECT_MASK List..."),back = True)
    # write only if it's a systempackage
    protect, mask = getConfigProtectAndMask()
    etpData['config_protect'] = protect
    etpData['config_protect_mask'] = mask

    print_info(yellow(" * ")+red("Getting Entropy API version..."),back = True)
    # write API info
    etpData['etpapi'] = etpConst['etpapi']
    
    # removing temporary directory
    os.system("rm -rf "+tbz2TmpDir)
    if (structuredLayout):
	if os.path.isdir(structuredPackageDir):
	    spawnCommand("rm -rf "+structuredPackageDir)

    print_info(yellow(" * ")+red("Done"),back = True)
    return etpData


def dependenciesTest(options):

    reagentRequestQuiet = False
    for opt in options:
	if opt.startswith("--quiet"):
	    reagentRequestQuiet = True

    if (not reagentRequestQuiet):
        print_info(red(" @@ ")+blue("ATTENTION: you need to have equo.conf properly configured only for your running repository !!"))
        print_info(red(" @@ ")+blue("Running dependency test..."))

    dbconn = databaseTools.etpDatabase(readOnly = True)
    
    # hey Equo, how are you?
    sys.path.append('../client')
    import equoTools
    equoTools.syncRepositories(quiet = reagentRequestQuiet)

    # get all the installed packages
    installedPackages = dbconn.listAllIdpackages()
    
    depsNotFound = {}
    depsNotSatisfied = {}
    # now look
    length = str((len(installedPackages)))
    count = 0
    for xidpackage in installedPackages:
	count += 1
	atom = dbconn.retrieveAtom(xidpackage)
	if (not reagentRequestQuiet):
	    print_info(darkred(" @@ ")+bold("(")+blue(str(count))+"/"+red(length)+bold(")")+darkgreen(" Checking ")+bold(atom), back = True)
	deptree, status = equoTools.generateDependencyTree([xidpackage,0])
	
	if (status == -2): # dependencies not found
	    depsNotFound[xidpackage] = []
	    for x in deptree:
		for z in deptree[x]:
		    for a in deptree[x][z]:
		        depsNotFound[xidpackage].append(a)
	    if (not depsNotFound[xidpackage]):
		del depsNotFound[xidpackage]

	if (status == 0):
	    depsNotSatisfied[xidpackage] = []
	    for x in range(len(deptree))[::-1]:
	        for z in deptree[x]:
		    depsNotSatisfied[xidpackage].append(z)
	    if (not depsNotSatisfied[xidpackage]):
		del depsNotSatisfied[xidpackage]
	
    packagesNeeded = []
    if (depsNotSatisfied):
	if (not reagentRequestQuiet):
            print_info(red(" @@ ")+blue("These are the packages that lack dependencies: "))
	for dict in depsNotSatisfied:
	    pkgatom = dbconn.retrieveAtom(dict)
	    if (not reagentRequestQuiet):
	        print_info(darkred("   ### ")+blue(pkgatom))
	    for dep in depsNotSatisfied[dict]:
		iddep = dep[0]
		repo = dep[1]
		depatom = dbconn.retrieveAtom(iddep)
		dbconn.closeDB()
		if (not reagentRequestQuiet):
		    print_info(bold("       :: ")+red(depatom))
		packagesNeeded.append([depatom,dep])

    packagesNotFound = []
    if (depsNotFound):
	if (not reagentRequestQuiet):
            print_info(red(" @@ ")+blue("These are the packages not found, that respective packages in repository need:"))
	for dict in depsNotFound:
	    pkgatom = dbconn.retrieveAtom(dict)
	    if (not reagentRequestQuiet):
	        print_info(darkred("   ### ")+blue(pkgatom))
	    for pkg in depsNotFound[dict]:
		if (not reagentRequestQuiet):
	            print_info(bold("       :: ")+red(pkg))
	        packagesNotFound.append(pkg)

    packagesNotFound = filterDuplicatedEntries(packagesNotFound)

    if (reagentRequestQuiet):
	for x in packagesNotFound:
	    print x

    dbconn.closeDB()
    return 0,packagesNeeded,packagesNotFound


def smartapps(options):

    reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"smartapps: called -> options: "+str(options))
    
    if (len(options) == 0):
        print_error(yellow(" * ")+red("No valid tool specified."))
	sys.exit(501)
    
    if (options[0] == "create"):
        myopts = options[1:]
	
	if (len(myopts) == 0):
	    print_error(yellow(" * ")+red("No packages specified."))
	    sys.exit(502)
	
	# open db
	dbconn = databaseTools.etpDatabase(readOnly = True)
	
	# seek valid apps (in db)
	validPackages = []
	for opt in myopts:
	    pkgsfound = dbconn.searchPackages(opt)
	    for pkg in pkgsfound:
		validPackages.append(pkg)

	dbconn.closeDB()

	if (len(validPackages) == 0):
	    print_error(yellow(" * ")+red("No valid packages specified."))
	    sys.exit(503)

        print_info(red(" @@ ")+blue("ATTENTION: you need to have equo.conf properly configured only for your running repository !!"))
        sys.path.append('../client')
        import equoTools
	equoTools.syncRepositories()

	# print the list
	print_info(green(" * ")+red("This is the list of the packages that would be worked out:"))
	for pkg in validPackages:
	    print_info(green("\t[SMART] - ")+bold(pkg[0]))

	rc = askquestion(">>   Would you like to create the packages above ?")
	if rc == "No":
	    sys.exit(0)
	
	for pkg in validPackages:
	    print_info(green(" * ")+red("Creating smartapp package from ")+bold(pkg[0]))
	    smartgenerator(pkg)

	print_info(green(" * ")+red("Smartapps creation done, remember to test them before publishing."))

    
# tool that generates .tar.bz2 packages with all the binary dependencies included
# @returns the package file path
# NOTE: this section is highly portage dependent
def smartgenerator(atomInfo):
    
    reagentLog.log(ETP_LOGPRI_INFO,ETP_LOGLEVEL_VERBOSE,"smartgenerator: called -> package: "+str(atomInfo))
    dbconn = databaseTools.etpDatabase(readOnly = True)

    sys.path.append('../client')
    import equoTools

    idpackage = atomInfo[1]
    atom = atomInfo[0]
    
    # check if the application package is available, otherwise, download
    pkgfilepath = dbconn.retrieveDownloadURL(idpackage)
    pkgcontent = dbconn.retrieveContent(idpackage)
    pkgfilename = os.path.basename(pkgfilepath)
    pkgname = pkgfilename.split(".tbz2")[0]
    
    pkgdependencies, result = equoTools.getRequiredPackages([[idpackage,etpConst['officialrepositoryname']]], emptydeps = True)
    # flatten them
    pkgs = []
    if (result == 0):
	for x in range(len(pkgdependencies))[::-1]:
	    #print x
	    for z in pkgdependencies[x]:
		#print treepackages[x][z]
		for a in pkgdependencies[x][z]:
		    pkgs.append(a)
    elif (result == -2):
	print_error(green(" * ")+red("Missing dependencies: ")+str(pkgdependencies))
	sys.exit(505)
    elif (result == -1):
	print_error(green(" * ")+red("Database file not found or no database connection. --> ")+str(pkgdependencies))
	sys.exit(506)

    print_info(green(" * ")+red("This is the list of the dependencies that would be included:"))
    for i in pkgs:
	atom = dbconn.retrieveAtom(i[0])
        print_info(green("    [] ")+red(atom))
	
    # create the working directory
    pkgtmpdir = etpConst['packagestmpdir']+"/"+pkgname
    #print "DEBUG: "+pkgtmpdir
    if os.path.isdir(pkgtmpdir):
	spawnCommand("rm -rf "+pkgtmpdir)
    os.makedirs(pkgtmpdir+"/content")
    mainBinaryPath = etpConst['packagesbindir']+"/"+pkgfilename
    print_info(green(" * ")+red("Unpacking main package ")+bold(str(pkgfilename)))
    uncompressTarBz2(mainBinaryPath,pkgtmpdir) # first unpack
    uncompressTarBz2(pkgtmpdir+etpConst['packagecontentdir']+"/"+pkgfilename,pkgtmpdir+"/content") # second unpack
    if os.path.isfile(pkgtmpdir+etpConst['packagecontentdir']+"/"+pkgfilename):
	os.remove(pkgtmpdir+etpConst['packagecontentdir']+"/"+pkgfilename)

    binaryExecs = []
    for file in pkgcontent:
	# remove /
	filepath = pkgtmpdir+"/content"+file
	import commands
	if os.access(filepath,os.X_OK):
	    # test if it's an exec
	    out = commands.getoutput("file "+filepath).split("\n")[0]
	    if out.find("LSB executable") != -1:
		binaryExecs.append(file)
	# check if file is executable


    # now uncompress all the rest
    contentdir = pkgtmpdir+"/content"
    for dep in pkgs:
	download = os.path.basename(dbconn.retrieveDownloadURL(dep[0]))
	print_info(green(" * ")+red("Unpacking dependency package ")+bold(str(download)))
	deppath = etpConst['packagesbindir']+"/"+download
	uncompressTarBz2(deppath,pkgtmpdir) # first unpack
	uncompressTarBz2(pkgtmpdir+etpConst['packagecontentdir']+"/"+download,contentdir) # second unpack
        if os.path.isfile(pkgtmpdir+etpConst['packagecontentdir']+"/"+download):
	    os.remove(pkgtmpdir+etpConst['packagecontentdir']+"/"+download)
	
	

    # remove unwanted files (header files)
    os.system('for file in `find '+contentdir+' -name "*.h"`; do rm $file; done')

    # now create the bash script for each binaryExecs
    os.makedirs(contentdir+"/wrp")
    bashScript = []
    bashScript.append(
    			'#!/bin/sh\n'
			'cd $1\n'

			'MYPYP=$(find $PWD/lib/python2.4/site-packages/ -type d -printf %p: 2> /dev/null)\n'
			'MYPYP2=$(find $PWD/lib/python2.5/site-packages/ -type d -printf %p: 2> /dev/null)\n'
			'export PYTHONPATH=$MYPYP:MYPYP2:$PYTHONPATH\n'

			'export PATH=$PWD:$PWD/sbin:$PWD/bin:$PWD/usr/bin:$PWD/usr/sbin:$PWD/usr/X11R6/bin:$PWD/libexec:$PWD/usr/local/bin:$PWD/usr/local/sbin:$PATH\n'
			
			'export LD_LIBRARY_PATH='
			'$PWD/lib:'
			'$PWD/lib64:'
			'$PWD/usr/lib:'
			'$PWD/usr/lib64:'
			'$PWD/usr/lib/nss:'
			'$PWD/usr/lib/nspr:'
			'$PWD/usr/lib64/nss:'
			'$PWD/usr/lib64/nspr:'
			'$PWD/usr/qt/3/lib:'
			'$PWD/usr/qt/3/lib64:'
			'$PWD/usr/kde/3.5/lib:'
			'$PWD/usr/kde/3.5/lib64:'
			'$LD_LIBRARY_PATH\n'
			
			'export KDEDIRS=$PWD/usr/kde/3.5:$PWD/usr:$KDEDIRS\n'
			
			'export PERL5LIB=$PWD/usr/lib/perl5:$PWD/share/perl5:$PWD/usr/lib/perl5/5.8.1'
			':$PWD/usr/lib/perl5/5.8.2:'
			':$PWD/usr/lib/perl5/5.8.3:'
			':$PWD/usr/lib/perl5/5.8.4:'
			':$PWD/usr/lib/perl5/5.8.5:'
			':$PWD/usr/lib/perl5/5.8.6:'
			':$PWD/usr/lib/perl5/5.8.7:'
			':$PWD/usr/lib/perl5/5.8.8:'
			':$PWD/usr/lib/perl5/5.8.9:'
			':$PWD/usr/lib/perl5/5.8.10\n'
			
			'export MANPATH=$PWD/share/man:$MANPATH\n'
			'export GUILE_LOAD_PATH=$PWD/share/:$GUILE_LOAD_PATH\n'
			'export SCHEME_LIBRARY_PATH=$PWD/share/slib:$SCHEME_LIBRARY_PATH\n'
			
			'# Setup pango\n'
			'MYPANGODIR=$(find $PWD/usr/lib/pango -name modules)\n'
			'if [ -n "$MYPANGODIR" ]; then\n'
			'    export PANGO_RC_FILE=$PWD/etc/pango/pangorc\n'
			'    echo "[Pango]" > $PANGO_RC_FILE\n'
			'    echo "ModulesPath=${MYPANGODIR}" >> $PANGO_RC_FILE\n'
			'    echo "ModuleFiles=${PWD}/etc/pango/pango.modules" >> $PANGO_RC_FILE\n'
			'    pango-querymodules > ${PWD}/etc/pango/pango.modules\n'
			'fi\n'
			'$2\n'
    )
    f = open(contentdir+"/wrp/wrapper","w")
    f.writelines(bashScript)
    f.flush()
    f.close()
    # chmod
    os.chmod(contentdir+"/wrp/wrapper",0755)



    # now list files in /sh and create .desktop files
    for file in binaryExecs:
	file = file.split("/")[len(file.split("/"))-1]
	runFile = []
	runFile.append(
			'#include <cstdlib>\n'
			'#include <cstdio>\n'
			'#include <stdio.h>\n'
			'int main() {\n'
			'  int rc = system(\n'
			'                "pid=$(pidof '+file+'.exe);"\n'
			'                "listpid=$(ps x | grep $pid);"\n'
			'                "filename=$(echo $listpid | cut -d\' \' -f 5);"'
			'                "currdir=$(dirname $filename);"\n'
			'                "/bin/sh $currdir/wrp/wrapper $currdir '+file+'" );\n'
			'  return rc;\n'
			'}\n'
	)
	f = open(contentdir+"/"+file+".cc","w")
	f.writelines(runFile)
	f.flush()
	f.close()
	# now compile
	spawnCommand("cd "+contentdir+"/ ; g++ -Wall "+file+".cc -o "+file+".exe")
	os.remove(contentdir+"/"+file+".cc")

    # now compress in .tar.bz2 and place in etpConst['smartappsdir']
    #print etpConst['smartappsdir']+"/"+pkgname+"-"+etpConst['currentarch']+".tar.bz2"
    #print pkgtmpdir+"/"
    compressTarBz2(etpConst['smartappsdir']+"/"+pkgname+"-"+etpConst['currentarch']+".tbz2",contentdir+"/")
    
    dbconn.closeDB()