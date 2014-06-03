#!/bin/python
"""
<Program Name>
  tcp_relay.repy

<Purpose>
  The purpose of this program is to act as a nat forwarder.
  Messages from nodeA to nodeB could be forwarded through
  this forwarder.

<Started>
  May 25th, 2011

<Author>
  Monzur Muhammad
  monzum@cs.washington.edu

<Usage>
  python repy.py RESTRICTION_FILE tcp_relay.repy TCP_PORT 
"""

from repyportability import *
add_dy_support(locals())

import sys

dy_import_module_symbols("session")
advertisepipe = dy_import_module("advertisepipe.r2py")
dy_import_module_symbols("affixstackinterface")
dy_import_module_symbols("tcp_relay_common_lib")
iplib = dy_import_module("checkprivateipaffix.repy")

# The affix string that the NAT Forwarder will use.
NAT_AFFIX_STRING = "(CoordinationAffix)(NoopAffix)"

# Time to sleep if socket is blocking.
SLEEP_TIME = 0.001

# How many bytes should be received at once.
RECV_BYTES = 2**10

# Some variables determining how many servers
# and clients can connect at once to the forwarder.
# Note that for each client that connects to a server,
# two new threads is started.
MAX_SERVERS = 10
MAX_CLIENTS_PER_SERVER = 5


# Message log types
INFO_MSG = 1
ERR_MSG = 2
DEBUG_MSG = 3


# A dictionary keeps track of all the servers that are registered
# and all the clients that are connected to each server.
# Dictionary has the format:
#    {'SERVERIP:PORT' : { 'connected_clients' : [(client_sockobj, client_id)],
#                         'waiting_clients' : [client_sockobj] }
registered_server = {}
register_lock = createlock()




# ====================================================
# TCP NAT Forwarder - Common Entry Point.
# ====================================================
def tcp_forwarder_listener():

  # Create a TCP server socket.
  affix_stack_object = AffixStackInterface(NAT_AFFIX_STRING)
  tcp_forwarder_sock = affix_stack_object.listenforconnection(getmyip(), mycontext['listenport_tcp'])
  
  logmsg("Started TCP NAT Forwarder listener on '%s' port '%d'" % 
         (getmyip(), mycontext['listenport_tcp']), INFO_MSG)
  logmsg("NAT Forwarder using affix string '%s'." % NAT_AFFIX_STRING, INFO_MSG)

  while True:
    try:
      # Try to see if there is any connection waiting.
      remote_ip, remote_port, sockobj = tcp_forwarder_sock.getconnection()
      logmsg("Incoming connection from '%s:%d'" % (remote_ip, remoteport), INFO_MSG)
    except SocketWouldBlockError:
      sleep(SLEEP_TIME)
    except Exception, err:
      logmsg("Error in getconnection: " + str(err), DEBUG_MSG)
    else:
      logmsg("Got connection from " + str(remote_ip) + ":" + str(remote_port), DEBUG_MSG)
      try:
        conn_init_message = session_recvmessage(sockobj)
        logmsg(str(remote_ip) + ":" + str(remote_port) + " said " + 
          conn_init_message, DEBUG_MSG)
        (conn_type, conn_id) = conn_init_message.split(',')
      except Exception, err:
        logmsg("Error in connection establishment: " + 
          str(type(err)) + " " + str(err), DEBUG_MSG)
        sockobj.close()
        continue
    
      if conn_type == SERVER_REGISTER:
        # This is the case where a new server wants to register to this
        # NAT Forwarder.
        createthread(register_new_server(remote_ip, remote_port, conn_id, sockobj))
        logmsg("Registered server " + remote_ip + ":" + str(remote_port), 
          DEBUG_MSG)
      elif conn_type == CONNECT_SERVER_TAG:
        # This is the case when a registered server opens up a connection to
        # the forwarder in order for it to be connected to a client.
        createthread(handle_server_conn_request(remote_ip, remote_port, conn_id, sockobj))
      elif conn_type == CONNECT_CLIENT_TAG:
        # Lanuch a new thread to deal with the new incoming client connection.
        createthread(handle_client_request(remote_ip, remote_port, conn_id, sockobj))
      else:
        logmsg("Incorrect connection type received from '%s:%d': %s" % 
              (remote_ip, remote_port, conn_type), ERR_MSG)
	  

    
    
    
def register_new_server(remote_ip, remote_port, server_id, sockobj):

  def _register_server_helper():
    """
    <Purpose>
      Register a new server with the forwarder. If we are here
      then a server node has called openconnection to the NAT
      forwarder for the first time in order to open up a connection
      such that the NAT forwarder can make future connections.
      
    <Arguments>
      remote_ip - The ip address of the node that made the initial connection.
      remote_port - The port number of the node that made the initial connection.
      sockobj - The socket that will be used for communication.
     
    <Exception>
      None.
	 
    <Side Effects>
      None.
	   
      <Return>
      None.
    """

    logmsg("Server '%s' requesting to register" % server_id, INFO_MSG)
    
    # Check to see if the server is already registered. If it is then 
    # we just return.
    if server_id in registered_server.keys():
      # Make sure that it is in the proper format.
      if 'connected_clients' not in registered_server[server_id].keys():
        registered_server[server_id]['connected_clients'] = []
      if 'waiting_clients' not in registered_server[server_id].keys():
        registered_server[server_id]['waiting_clients'] = []
      if 'client_lock' not in registered_server[server_id].keys():
        registered_server[server_id]['client_lock'] = createlock()
      return
  
  
    # Acquire the lock before registering so there is no contention.
    register_lock.acquire(True)
    try:
      if len(registered_server.keys()) < MAX_SERVERS:
        # The server_id does not exist in our registered_server dict.
        registered_server[server_id] = {}
        registered_server[server_id]['connected_clients'] = []
        registered_server[server_id]['waiting_clients'] = []
        registered_server[server_id]['client_lock'] = createlock()
        
        # Launch a thread that waits for any communication from the server.
        # Such that if a server checks to see if there is any client waiting,
        # it responds.
        createthread(launch_server_communication_thread(sockobj, server_id))
        
        try:
          session_sendmessage(sockobj, CONNECT_SUCCESS)
        except SocketClosedRemote:
          unregister_server(server_id)

        logmsg("Registered server '%s' successfully." % server_id, INFO_MSG)
      else:
        session_sendmessage(sockobj, CONNECT_FAIL)
        logmsg("Unable to register server '%s'. Max servers reached." % server_id, INFO_MSG)
    finally:
      register_lock.release()
      
      
  return _register_server_helper
  
  
  
def handle_server_conn_request(remote_ip, remote_port, server_id, sockobj):  

  def _handle_server_conn_request_helper():
    """
    <Purpose>
      A connection is made to the NAT Forwarder because there
      is a client that is waiting to be connected to the server.
      Once this connection is made, the server and client will
      be connected through the forwarder and will be able to 
      communicate with each other.
    
    <Arguments>
      remote_ip - The ip address of the node that made the initial connection.
      remote_port - The port number of the node that made the initial connection.
      sockobj - The socket that will be used for communication.
     
    <Exception>
      None.
	 
    <Side Effects>
      None.
	   
      <Return>
      None.
    """
    logmsg("Server '%s' has made a connection in order for a client to connect." %
            server_id, INFO_MSG)
            
    # Check to make sure that the server has registered already.
    if server_id not in registered_server.keys():
      session_sendmessage(sockobj, CONNECT_FAIL)
      logmsg("Server '%s' attempting to connect to client before registering" %
              server_id, ERR_MSG)
      return
   
   
    # Acquire a client lock to ensure there is no contention.
    registered_server[server_id]['client_lock'].acquire(True)  
    try:
      if len(registered_server[server_id]['connected_clients']) < MAX_CLIENTS_PER_SERVER:
        # Retrieve the list of clients that are waiting.
        client_queue = registered_server[server_id]['waiting_clients']
        (cur_client_sockobj, client_id) = client_queue.pop()
    
        # Start up two threads that can forward messages to each other.
        # We also need locks for each of the sockets as the two threads
        # will attempt to simulatenously recv and send through it.
        createthread(forward_tcp_message(server_id, client_id, sockobj, cur_client_sockobj))
        createthread(forward_tcp_message(server_id, client_id, cur_client_sockobj, sockobj))
        
        # Place the client socket in the connected clients list.
        registered_server[server_id]['connected_clients'].append(cur_client_sockobj)
        
        # We have made a connection so we send a Connect Success message to both the
        # server and the client. However the client might have already closed the 
        # connection due to having to wait for a long time. In this case we have to
        # catch the exception. If an exception is raised, we do nothing as everything
        # will be cleaned up when the forward_tcp_message thread tries to forward any
        # messages.
        try:
          session_sendmessage(sockobj, CONNECT_SUCCESS + ',' + client_id)
        except (SocketClosedRemote, SocketClosedLocal), err:
          # If the server connection has been closed, we unregister
          # the server.
          register_lock.acquire(True)
          try:
            unregister_server(server_id)
          finally:
            register_lock.release()
        try:
          session_sendmessage(cur_client_sockobj, CONNECT_SUCCESS)
        except (SocketClosedRemote, SocketClosedLocal), err:
          logmsg("Couldn't connect server '%s' to client '%s'. Client closed connection. %s" %
                (server_id, client_id, str(err)), ERR_MSG)
        logmsg("Made connection to '%s' from '%s'" % (server_id, client_id), INFO_MSG)
      else:
        session_sendmessage(sockobj, CONNECT_FAIL)
        logmsg("Unable to register any client to '%s'. Max clients received on server." % 
              server_id, INFO_MSG)
    except Exception, err:
      session_sendmessage(sockobj, CONNECT_FAIL)
      logmsg("Unable to connect a client to '%s' due to err. %s" % (server_id, str(err)))
    finally:
      registered_server[server_id]['client_lock'].release()
  
  return _handle_server_conn_request_helper
  
  
  
  
def handle_client_request(remote_ip, remote_port, server_id, sockobj):

  def _handle_client_request_helper():
    """
    <Purpose>
      Take an incoming connection from a client and append it to the 
      registered servers waiting list. Then wait for the server to be
      connected to sthe client.
	 
    <Arguments>
      remote_ip - The ip address of the node that made the initial connection.
      remote_port - The port number of the node that made the initial connection.
      sockobj - The socket that will be used for communication.
     
    <Exception>
      None.
	 
    <Side Effects>
      None.
	   
      <Return>
      None.
    """
    client_id = "%s:%d" % (remote_ip, remote_port)
    logmsg("Incoming TCP connection request from '%s' for server '%s'" % 
           (client_id, server_id), INFO_MSG)
    
    if server_id not in registered_server.keys():
      session_sendmessage(sockobj, CONNECT_FAIL)
      logmsg("Client '%s' attempting to connect to unregistered server '%s'" %
            (client_id, server_id), ERR_MSG)
      return
    
    
    # Acquire the client lock for the server and add the socket to 
    # the waiting queue. Note that we add the client at index 0 since
    # pop() will remove the client from the queue from the tail of the
    # list.
    registered_server[server_id]['client_lock'].acquire(True)
    try:
      registered_server[server_id]['waiting_clients'].insert(0, (sockobj, client_id))
    finally:
      registered_server[server_id]['client_lock'].release()
    
  
  return _handle_client_request_helper



  
# ==============================================================
# TCP Forwarder - Actual Message Forwarding
# ==============================================================
def forward_tcp_message(server_id, client_id, from_sock, to_sock):
  
  def _forward_tcp_message_helper():
    """
    <Purpose>
      The function forwards all the incoming messages from one
	  socket to another.
	
    <Arguments>
      from_sock - The socket to listen on.
	  to_sock - The socket to forward to.
	
    <Side Effects>
      None

    <Exceptions>	
	  None

    <Return>
      None
    """
    

    data_recv = ''

    while True:
      try:
        # Receive the incoming message, then forward the entire message
        # to the to_sock. But first we attempt to acquire the lock
        data_recv += from_sock.recv(RECV_BYTES)
      except SocketWouldBlockError:
        sleep(SLEEP_TIME)
      except (SocketClosedLocal, SocketClosedRemote):
        # If any of the socket is closed then we break out of the loop.
        logmsg("Receiving socket '%s' was closed either locally or remotely." % str(from_sock), DEBUG_MSG)
        break

      if data_recv:
        try:
          data_sent = to_sock.send(data_recv)
          data_recv = data_recv[data_sent:]
        except SocketWouldBlockError:
          sleep(SLEEP_TIME)
        except (SocketClosedLocal, SocketClosedRemote):
          # If any of the socket is closed then we break out of the loop.
          logmsg("Sending socket '%s' was closed either locally or remotely." % str(to_sock), DEBUG_MSG)
          break
  
    """
    while True:
      try:
        # Receive the incoming message, then forward the entire message
        # to the to_sock. But first we attempt to acquire the lock
        data_recv = from_sock.recv(RECV_BYTES)
        
        # Send all the data.
        while data_recv:
          try:
            data_sent = to_sock.send(data_recv)
            data_recv = data_recv[data_sent:]
          except SocketWouldBlockError:
            sleep(SLEEP_TIME)
      except SocketWouldBlockError:
        sleep(SLEEP_TIME)
      except (SocketClosedLocal, SocketClosedRemote):
        # If any of the socket is closed then we break out of the loop.
        break
      except Exception, err:
        logmsg("Error in forwarding TCP message. " + str(err), DEBUG_MSG)
      """
    # Since we are done sending the data, we clean up the sockets. Note
    # that we should only be here if one of the socket has already been
    # closed. 
    # Note that if from_sock was the one that was closed, then it will throw
    # an exception when we try to close it and we will not get to to_sock.close().
    # This however is fine as there will be two instances of this thread running
    # and the to_sock from this thread will be the from_sock in the other thread
    # which means one of the threads will ensure that each of the sockets is closed.
    try:
      from_sock.close()
      to_sock.close()
    except:
      pass
      
    # We also remove the client socket from the connected clients list for
    # the server. Note that we don't know if the from_sock or to_sock is the
    # client socket, so we attempt to remove both.
    # Similar to the socket case above, even if an exception is raised when
    # we attempt to remove from_sock, to_sock will be removed by the other
    # thread that is running. That is, one or both of the threads will ensure
    # that we attempt to remove both the from_sock and to_sock from the 
    # connected_clients list.
    try:
      registered_server[server_id]['connected_clients'].remove(from_sock)
      registered_server[server_id]['connected_clients'].remove(to_sock)
    except:
      pass
     
    logmsg("Connection terminated between server '%s' and client '%s'" % (server_id, client_id), INFO_MSG)



  # Return the helper function.	  
  return _forward_tcp_message_helper 	  

		
	
  
# ====================================================
# TCP Server Control Socket
# ====================================================
def launch_server_communication_thread(sockobj, server_id):
  
  def _server_communication_helper():
    """
    <Purpose>
      This thread is launched after a server has registered with
      the nat forwarder. This thread will keep running until the
      control socket for the server has been closed. It is used
      for communicating with the server. The server may ping from
      time to time to check if there are any clients waiting for
      it. If there are then the server will create a connection
      with the Nat Forwarder.
      
    <Arguments>
      sockobj - The socket object that is used for communication.
      server_id - The id of the server
    
    <Side Effects>
      None
      
    <Exceptions>
      None
      
    <Return>
      None
    """
    
    # Keep this thread alive as long as the socket object is open.
    # We break out of the loop if there is any socket closed exceptions
    # or any unexpected errors that arise.
    while True:
      try:
        request_type = session_recvmessage(sockobj)
        # Check if the server is requesting to see if there is any
        # clients waiting to be connected.
        if request_type == CHECK_SERVER_CONN:
          if len(registered_server[server_id]['waiting_clients']) > 0:
            session_sendmessage(sockobj, CLIENT_AVAILABLE)
          else:
            session_sendmessage(sockobj, CLIENT_UNAVAILABLE)
      except (SocketClosedRemote, SocketClosedLocal, SessionEOF), err:
        break
      except Exception, err:
        logmsg("Unexpected error in launch_server_communication_thread: " + 
          str(type(err)) + " " + str(err), ERR_MSG)
        break

    sockobj.close()

    # Once we have broken out of the loop, we are going to unregister
    # the server before we exit this thread.
    register_lock.acquire(True)
    try:
      unregister_server(server_id)
      registered_server.pop(server_id)
    except KeyError:
      # If the key is not in the dictionary, then we don't have
      # to worry about it.
      pass
    finally:
      register_lock.release()
      
    logmsg("Unregistered server '%s'." % server_id, INFO_MSG) 
  
  return _server_communication_helper
    




def unregister_server(server_id):

  if server_id not in registered_server.keys():
    return

  registered_server[

    
    
# ====================================================
# Common
# ====================================================
def logmsg(message, msg_type):

  header = "[%.4f] " % getruntime()

  if msg_type == INFO_MSG:
    header += "INFO: "
  elif msg_type == ERR_MSG:
    header += "ERROR: "
  elif msg_type == DEBUG_MSG:
    header += "DEBUG: "

  log(header + message + '\n')
  sys.stdout.flush()


# ====================================================
# Program Entry
# ====================================================
if __name__ == '__main__':
  logmsg("Starting unrestricted NAT forwarder.", INFO_MSG)

  if len(sys.argv) < 2:
    print "Usage:\n\tpython run_unrestricted_tcp_relay.py TCP_PORT [NAT_AFFIX_STRING]"
    sys.exit(1)

  mycontext['listenport_tcp'] = int(sys.argv[1])

  if len(sys.argv) >= 3:
    NAT_AFFIX_STRING = sys.argv[2]

  myip, myport = getmyip(), str(mycontext['listenport_tcp'])

  if iplib.is_private_ipv4_address(getmyip()):
    logmsg(
"""NOTE WELL: You are trying to run a NAT forwarder on a private IP address. 
This leaves the forwarder uncontactable from the public Internet unless 
you set up port forwarding etc. on your NAT gateway. I'll let you proceed 
regardless. You hopefully know what you do.""", ERR_MSG)
  
  # Launch the TCP Forwarder.
  logmsg("Creating forwarder thread on " + myip + ":" + str(myport), INFO_MSG)
  createthread(tcp_forwarder_listener)
  
  # Launch advertiser and advertise this forwarders ip address, tcp port.
  advertise_value = myip + ':' + myport 
  logmsg("Starting advertise thread for " + NAT_FORWARDER_KEY + 
    ": " + advertise_value, INFO_MSG)
  advertisepipe.add_to_pipe(NAT_FORWARDER_KEY, advertise_value)