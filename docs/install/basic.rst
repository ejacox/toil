.. highlight:: console

.. _installation-ref:

Installation
============

This document describes how to prepare for and install the Toil software. Note that we recommend running all the Toil commands inside a Python virtual environment.  Instructions for installing and creating a Python virtual environment are provided below.

Preparation
-----------

Toil supports only Python 2.7.  If you don't satisfy this requirement, consider using anaconda_ to create an alternate Python 2.7 installation.

.. _anaconda: https://conda.io/docs/py2or3.html 

Install Python ``virtualenv`` using pip_. 
::

    $ sudo pip install virtualenv
    
.. _pip: https://pip.readthedocs.io/en/latest/installing/

Create a virtual environment called ``venv`` in your home directory.
::

    $ virtualenv ~/venv

Activate your virtual environment.
::

    $ source ~/venv/bin/activate
   

Basic installation
------------------

Toil can be easily installed using pip::

    $ pip install toil


.. _extras:

Extras
------

Some optional features, called *extras*, are not included in the basic
installation of Toil. To install Toil with all its bells and whistles, run

::

    $ pip install toil[aws,mesos,azure,google,encryption,cwl]

Here's what each extra provides:

+----------------+------------------------------------------------------------+
| Extra          | Description                                                |
+================+============================================================+
| ``aws``        | Provides support for storing workflow state in Amazon AWS. |
|                | This extra has no native dependencies.                     |
+----------------+------------------------------------------------------------+
| ``google``     | Experimental. Stores workflow state in Google Cloud        |
|                | Storage. This extra has no native dependencies.            |
+----------------+------------------------------------------------------------+
| ``azure``      | Stores workflow state in Microsoft Azure Storage. This     |
|                | extra has no native dependencies.                          |
+----------------+------------------------------------------------------------+
| ``mesos``      | Provides support for running Toil on an `Apache Mesos`_    |
|                | cluster. Note that running Toil on SGE (GridEngine),       |
|                | Parasol, or a single machine does not require an extra.    |
|                | The ``mesos`` extra requires the following native          |
|                | dependencies:                                              |
|                |                                                            |
|                | * `Apache Mesos`_ (Tested with Mesos v1.0.0)               |
|                | * :ref:`Python headers and static libraries <python-dev>`  |
|                |                                                            |
|                | .. important::                                             |
|                |    If you want to install Toil with the ``mesos`` extra    |
|                |    in a virtualenv, be sure to create that virtualenv with |
|                |    the ``--system-site-packages`` flag::                   |
|                |                                                            |
|                |       $ virtualenv --system-site-packages                  |
|                |                                                            |
|                |    Otherwise, you'll see something like this:              |
|                |                                                            |
|                |    .. code-block:: python                                  |
|                |                                                            |
|                |        ImportError: No module named mesos.native           |
|                |                                                            |
+----------------+------------------------------------------------------------+
| ``encryption`` | Provides client-side encryption for files stored in the    |
|                | Azure and AWS job stores. This extra requires the following|
|                | native dependencies:                                       |
|                |                                                            |
|                | * :ref:`Python headers and static libraries <python-dev>`  |
|                | * :ref:`libffi headers and library <libffi-dev>`           |
+----------------+------------------------------------------------------------+
| ``cwl``        | Provides support for running workflows written using the   |
|                | `Common Workflow Language`_.                               |
+----------------+------------------------------------------------------------+

.. _python-dev:
.. topic:: Python headers and static libraries

   Only needed for the ``mesos`` and ``encryption`` extras. On Ubuntu::

      $ sudo apt-get install build-essential python-dev

   On macOS::

      $ xcode-select --install

.. _libffi-dev:
.. topic:: libffi headers and library

   Only needed for the ``encryption`` extra. On Ubuntu::

      $ sudo apt-get install libffi-dev

   On macOS::

      $ brew install libffi


.. _Apache Mesos: https://mesos.apache.org/gettingstarted/
.. _Homebrew: http://brew.sh/
