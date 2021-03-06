#!/usr/bin/env python

'''
To run code: roslaunch comp_robo_project2 test_comp_robo_project2.launch map_file:=`rospack find comp_robo_project2`/maps/playground.yaml use_sim_time:=true

To run rviz: roslaunch turtlebot_rviz_launchers view_navigation.launch

To run tele_op: rosrun teleop_twist_keyboard teleop_twist_keyboard.py
To run code (small playground): roslaunch comp_robo_project2 test_comp_robo_project2.launch map_file:=`rospack find comp_robo_project2`/maps/playground_smaller.yaml use_sim_time:=true

To run simulator: roslaunch neato_simulator neato_tb_playground.launch 

To connect to neato: roslaunch neato_node bringup.launch host:=192.168.17.207
'''

import rospy

from std_msgs.msg import Header, String
from sensor_msgs.msg import LaserScan
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion
from nav_msgs.srv import GetMap

import tf
from tf import TransformListener
from tf import TransformBroadcaster
from tf.transformations import euler_from_quaternion, rotation_matrix, quaternion_from_matrix, quaternion_from_euler
from random import gauss
from copy import deepcopy

import math
import time
import random

import numpy as np
from numpy.random import random_sample
from sklearn.neighbors import NearestNeighbors

class TransformHelpers:
	""" Some convenience functions for translating between various representions of a robot pose.
		TODO: nothing... you should not have to modify these """

	@staticmethod
	def convert_translation_rotation_to_pose(translation, rotation):
		""" Convert from representation of a pose as translation and rotation (Quaternion) tuples to a geometry_msgs/Pose message """
		return Pose(position=Point(x=translation[0],y=translation[1],z=translation[2]), orientation=Quaternion(x=rotation[0],y=rotation[1],z=rotation[2],w=rotation[3]))

	@staticmethod
	def convert_pose_inverse_transform(pose):
		""" Helper method to invert a transform (this is built into the tf C++ classes, but ommitted from Python) """
		translation = np.zeros((4,1))
		translation[0] = -pose.position.x
		translation[1] = -pose.position.y
		translation[2] = -pose.position.z
		translation[3] = 1.0

		rotation = (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w)
		euler_angle = euler_from_quaternion(rotation)
		rotation = np.transpose(rotation_matrix(euler_angle[2], [0,0,1]))		# the angle is a yaw
		transformed_translation = rotation.dot(translation)

		translation = (transformed_translation[0], transformed_translation[1], transformed_translation[2])
		rotation = quaternion_from_matrix(rotation)
		return (translation, rotation)

	@staticmethod
	def convert_pose_to_xy_and_theta(pose):
		""" Convert pose (geometry_msgs.Pose) to a (x,y,yaw) tuple """
		orientation_tuple = (pose.orientation.x, pose.orientation.y, pose.orientation.z, pose.orientation.w)
		angles = euler_from_quaternion(orientation_tuple)
		return (pose.position.x, pose.position.y, angles[2])

class Particle:
	""" Represents a hypothesis (particle) of the robot's pose consisting of x,y and theta (yaw)
		Attributes:
			x: the x-coordinate of the hypothesis relative to the map frame
			y: the y-coordinate of the hypothesis relative ot the map frame
			theta: the yaw of the hypothesis relative to the map frame
			w: the particle weight (the class does not ensure that particle weights are normalized
	"""

	def __init__(self,x=0.0,y=0.0,theta=0.0,w=1.0):
		""" Construct a new Particle
			x: the x-coordinate of the hypothesis relative to the map frame
			y: the y-coordinate of the hypothesis relative ot the map frame
			theta: the yaw of the hypothesis relative to the map frame
			w: the particle weight (the class does not ensure that particle weights are normalized """ 
		self.w = w
		self.theta = theta
		self.x = x
		self.y = y

	def as_pose(self):
		""" A helper function to convert a particle to a geometry_msgs/Pose message """
		orientation_tuple = tf.transformations.quaternion_from_euler(0,0,self.theta)
		return Pose(position=Point(x=self.x,y=self.y,z=0), orientation=Quaternion(x=orientation_tuple[0], y=orientation_tuple[1], z=orientation_tuple[2], w=orientation_tuple[3]))

class OccupancyField:
	""" Stores an occupancy field for an input map.  An occupancy field returns the distance to the closest
		obstacle for any coordinate in the map
		Attributes:
			map: the map to localize against. Known unoccupied cells are white, obstacles are white, and unknown is grey (nav_msgs/OccupancyGrid)
			closest_occ: the distance for each entry in the OccupancyGrid to the closest obstacle
	"""

	def __init__(self, map):
		print "OccupancyField initializing"
		self.map = map		# save this for later
		self.resolution = self.map.info.resolution
		self.origin = self.map.info.origin #to get ge the x coordinate of the origin write self.origin.position.x
		# build up a numpy array of the coordinates of each grid cell in the map
		X = np.zeros((self.map.info.width*self.map.info.height,2))

		# while we're at it let's count the number of occupied cells
		total_occupied = 0
		curr = 0
		self.unoccupied_cells = [] # Array of cells (described by a tuple of indices) that are not inhabited by an obsacle
		for i in range(self.map.info.width):
			for j in range(self.map.info.height):
				# occupancy grids are stored in row major order, if you go through this right, you might be able to use curr
				ind = i + j*self.map.info.width
				if self.map.data[ind] > 0:
					total_occupied += 1
				elif self.map.data[ind] == 0: # Unoccpied cells are white
					self.unoccupied_cells.append([float(i),float(j)])
				X[curr,0] = float(i)
				X[curr,1] = float(j)
				curr += 1

		# build up a numpy array of the coordinates of each occupied grid cell in the map
		O = np.zeros((total_occupied,2))
		curr = 0
		for i in range(self.map.info.width):
			for j in range(self.map.info.height):
				# occupancy grids are stored in row major order, if you go through this right, you might be able to use curr
				ind = i + j*self.map.info.width
				if self.map.data[ind] > 0:
					O[curr,0] = float(i)
					O[curr,1] = float(j)
					curr += 1

					
		# use super fast scikit learn nearest neighbor algorithm
		nbrs = NearestNeighbors(n_neighbors=1,algorithm="ball_tree").fit(O)
		distances, indices = nbrs.kneighbors(X)

		self.closest_occ = {}
		curr = 0
		for i in range(self.map.info.width):
			for j in range(self.map.info.height):
				ind = i + j*self.map.info.width
				self.closest_occ[ind] = distances[curr][0]*self.map.info.resolution
				curr += 1

		print "OccupancyField initialized"

	def get_closest_obstacle_distance(self,x,y):
		""" (x,y) is in meters. Compute the closest obstacle to the specified (x,y) coordinate in the map.  If the (x,y) coordinate
			is out of the map boundaries, nan will be returned. """
		x_coord = int((x - self.map.info.origin.position.x)/self.map.info.resolution)
		y_coord = int((y - self.map.info.origin.position.y)/self.map.info.resolution)

		# check if we are in bounds
		if x_coord > self.map.info.width or x_coord < 0:
			return float('nan')
		if y_coord > self.map.info.height or y_coord < 0:
			return float('nan')

		ind = x_coord + y_coord*self.map.info.width
		if ind >= self.map.info.width*self.map.info.height or ind < 0:
			return float('nan')
		return self.closest_occ[ind]

class ParticleFilter:
	""" The class that represents a Particle Filter ROS Node
		Attributes list:
			initialized: a Boolean flag to communicate to other class methods that initializaiton is complete
			base_frame: the name of the robot base coordinate frame (should be "base_link" for most robots)
			map_frame: the name of the map coordinate frame (should be "map" in most caPose(ses)
			odom_frame: the name of the odometry coordinate frame (should be "odom" in most cases)
			scan_topic: the name of the scan topic to listen to (should be "scan" in most cases)
			n_particles: the number of particles in the filter
			d_thresh: the amount of linear movement before triggering a filter update
			a_thresh: the amount of angular movement before triggering a filter update
			laser_max_distance: the maximum distance to an obstacle we should use in a likelihood calculation
			pose_listener: a subscriber that listens for new approximate pose estimates (i.e. generated through the rviz GUI)
			particle_pub: a publisher for the particle cloud
			laser_subscriber: listens for new scan data on topic self.scan_topic
			tf_listener: listener for coordinate transforms
			tf_broadcaster: broadcaster for coordinate transforms
			particle_cloud: a list of particles representing a probability distribution over robot poses
			current_odom_xy_theta: the pose of the robot in the odometry frame when the last filter update was performed.
								   The pose is expressed as a list [x,y,theta] (where theta is the yaw)
			map: the map we will be localizing ourselves in.  The map should be of type nav_msgs/OccupancyGrid
	"""
	def __init__(self):
		print "ParticleFilter initializing "
		self.initialized = False		# make sure we don't perform updates before everything is setup
		rospy.init_node('comp_robo_project2')			# tell roscore that we are creating a new node named "pf"

		self.base_frame = "base_link"		# the frame of the robot base
		self.map_frame = "map"			# the name of the map coordinate frame
		self.odom_frame = "odom"		# the name of the odometry coordinate frame
		self.scan_topic = "scan"		# the topic where we will get laser scans from 

		self.n_particles = 200			# the number of paporticles to use

		self.d_thresh = 0.1				# the amount of linear movement before performing an update
		self.a_thresh = math.pi/12		# the amount of angular movement before performing an update

		self.laser_max_distance = 2.0	# maximum penalty to assess in the likelihood field model

		# Setup pubs and subs

		# pose_listener responds to selection of a new approximate robot location (for instance using rviz)
		self.pose_listener = rospy.Subscriber("initialpose", PoseWithCovarianceStamped, self.update_initial_pose)
		# publish the current particle cloud.  This enables viewing particles in rviz.
		self.particle_pub = rospy.Publisher("particlecloud", PoseArray)
		self.pose_pub = rospy.Publisher("predictedPose", PoseArray)
		self.scan_shift_pub = rospy.Publisher("scanShift", PoseArray)

		# laser_subscriber listens for data from the lidar
		self.laser_subscriber = rospy.Subscriber(self.scan_topic, LaserScan, self.scan_received)

		# enable listening for and broadcasting coordinate transforms
		self.tf_listener = TransformListener()
		self.tf_broadcaster = TransformBroadcaster()

		print "waiting for map server"
		rospy.wait_for_service('static_map')
		print "static_map service loaded"
		static_map = rospy.ServiceProxy('static_map', GetMap)
		worldMap = static_map()

		if worldMap:
			print "obtained map"

		# for now we have commented out the occupancy field initialization until you can successfully fetch the map
		self.occupancy_field = OccupancyField(worldMap.map)
		self.initialized = True
		print "ParticleFilter initialized"



	def update_robot_pose(self):
		""" Update the estimate of the robot's pose given the updated particles.
			There are two logical methods for this:
				(1): compute the mean pose (level 2)
				(2): compute the most likely pose (i.e. the mode of the distribution) (level 1)
		"""
		highestWeight = 0
		highestIndex = 0

		weightsAndParticles = [] # array of tuples of the weight of each particle and the particle itself

		for i in range(len(self.particle_cloud)):
			weightsAndParticles.append((self.particle_cloud[i].w,self.particle_cloud[i]))

		# Order by weights
		sorted_by_first = sorted(weightsAndParticles, key=lambda tup: tup[0])[::-1]

		# Select the the top third of particles with the hightes weights (probablilities)
		topParticles = [i[1] for i in sorted_by_first][:int(self.n_particles*.3)]

		# Average the top wighted particles to be the guessed position
		self.robot_pose = self.averageHypos(topParticles)

	def averageHypos(self, hypoList):
		""" Averages the positions and angles of the input Particles
			hypoList must be a list of Particles
			returns Particle position info
		"""
		xList = []
		yList = []
		thetaList = []
		
		if hypoList == [] or hypoList == None:
			print "hypoList is invalid"
			return Particle(x=0,y=0,theta=0,w=0).as_pose()
		
		# Sort particles' characteristics in appropriate lists
		for particle in hypoList:
			xList.append(particle.x)
			yList.append(particle.y)
			thetaList.append(particle.theta)

		# Average X and Y positions
		averageX = sum(xList)/len(xList)
		averageY = sum(yList)/len(yList)


		# Average angles by decomposing vectors, averaging components, and converting back to an angle
		unitXList = []
		unitYList = []
		for theta in thetaList:
			unitXList.append(math.cos(theta))
			unitYList.append(math.sin(theta))
		averageUnitX = sum(unitXList)/len(unitXList)
		averageUnitY = sum(unitYList)/len(unitYList)
		averageTheta = (math.atan2(averageUnitY,averageUnitX)+(2*math.pi))%(2*math.pi)

		return Particle(x=averageX,y=averageY,theta=averageTheta ,w=1.0).as_pose()

	def update_particles_with_odom(self, msg):
		""" Update the particles using the newly given odometry pose.
			The function computes the value delta which is a tuple (x,y,theta)
			that indicates the change in position and angle between the odometry
			when the particles were last updated and the current odometry.

			msg: this is not really needed to implement this, but is here just in case.
		"""
		new_odom_xy_theta = TransformHelpers.convert_pose_to_xy_and_theta(self.odom_pose.pose)
		# compute the change in x,y,theta since our last update
		if self.current_odom_xy_theta:
			old_odom_xy_theta = self.current_odom_xy_theta
			delta = (new_odom_xy_theta[0] - self.current_odom_xy_theta[0], new_odom_xy_theta[1] - self.current_odom_xy_theta[1], new_odom_xy_theta[2] - self.current_odom_xy_theta[2])
			self.current_odom_xy_theta = new_odom_xy_theta
		else:
			self.current_odom_xy_theta = new_odom_xy_theta
			return

		# assumes map centered at 0,0
		x_max_boundary = -self.occupancy_field.origin.position.x
		x_min_boundary = self.occupancy_field.origin.position.x
		y_max_boundary = -self.occupancy_field.origin.position.y
		y_min_boundary = self.occupancy_field.origin.position.y

		# Loops through particles to upsade with odom information
		for i in range(len(self.particle_cloud)):
			# Calculates amount of change for angle and X and Y position
			tempDelta = self.rotatePositionChange(old_odom_xy_theta, delta, self.particle_cloud[i])
			self.particle_cloud[i].x += tempDelta[0]
			self.particle_cloud[i].y += tempDelta[1]
			self.particle_cloud[i].theta += tempDelta[2]
			# Accounts for angle wrapping
			if self.particle_cloud[i].theta > (2*math.pi) or self.particle_cloud[i].theta < 0:
				self.particle_cloud[i].theta = self.particle_cloud[i].theta%(2*math.pi)

			#check map boundaries. Any particles no longer within map boundaries are moved to boundary
			if self.particle_cloud[i].x > x_max_boundary:
				self.particle_cloud[i].x = x_max_boundary
			elif self.particle_cloud[i].x < x_min_boundary:
				self.particle_cloud[i].x = x_min_boundary
			if self.particle_cloud[i].y > y_max_boundary: 
				self.particle_cloud[i].y = y_max_boundary
			elif self.particle_cloud[i].y < y_min_boundary:
				self.particle_cloud[i].y = y_min_boundary

		# For added difficulty: Implement sample_motion_odometry (Prob Rob p 136).

	def rotatePositionChange(self,old_odom_xy_theta, delta, particle):
		""" Determines the change in X and Y for each hypothesis based on its angle """
		angle = particle.theta - old_odom_xy_theta[2]
		newDeltaX = delta[0]*math.cos(angle) - delta[1]*math.sin(angle)
		newDeltaY = delta[0]*math.sin(angle) + delta[1]*math.cos(angle)
		return (newDeltaX,newDeltaY,delta[2])

	def map_calc_range(self,x,y,theta):
		""" Difficulty Level 3: implement a ray tracing likelihood model... Let me know if you are interested """
		# TODO: nothing unless you want to try this alternate likelihood model
		pass

	def resample_particles(self):
		""" Resample the particles according to the new particle weights.
			The weights stored with each particle should define the probability that a particular
			particle is selected in the resampling step.  You may want to make use of the given helper
			function draw_random_sample.
		"""
		weights = []
		choices = []
		probabilities = []
		
		# Sort particle cloud info into appropriate arrays
		for particle in self.particle_cloud:
			choices.append(particle)
			probabilities.append(particle.w)

		# Only resample 2/3 of the original number of particles from current pool 
		numParticles = int(self.n_particles/3)*2

		# Randomly draw particles from the current particle cloud biased towards points with higher weights
		temp_particle_cloud = self.draw_random_sample(choices, probabilities, numParticles)

		# Add uncertaintly/noise to all points
		for particle in self.particle_cloud:
			particle.x  = particle.x + random.gauss(0, .1)
			particle.y  = particle.y + random.gauss(0, .1)
			particle.theta  = particle.theta + random.gauss(0, .4)

		# Randomly pick the remaining 1/3 of particles randomly from known unoccupied cells of map, then 
		# combine with the 2/3 biasedly chosen earlier
		self.particle_cloud = temp_particle_cloud + self.generateRandomParticles(self.n_particles - numParticles)


	def generateRandomParticles(self, number):
		""" Generates random particles from the unoccupied portion of the map
			Returns array of random particles lengh of input number
		"""
		res = self.occupancy_field.map.info.resolution
		temp_particle_cloud = []
		unoccupied_cells = self.occupancy_field.unoccupied_cells

		for i in range(number):
			random_pt_index = int(random.uniform(0,len(unoccupied_cells)))
			x = (unoccupied_cells[random_pt_index][0] ) * res + self.occupancy_field.origin.position.x
			y = (unoccupied_cells[random_pt_index][1] ) * res + self.occupancy_field.origin.position.y
			theta = random.uniform(0,2*math.pi)
			rand_particle = Particle(x = x, y = y, theta =  theta)
			temp_particle_cloud.append(rand_particle)

		return temp_particle_cloud


	def update_particles_with_laser(self, msg):
		""" Updates the particle weights in response to the scan contained in the msg """
		# TODO: implement this
		scanList = []
		pointList = []
		# create list of valid scans
		for i in range(len(msg.ranges)):
			if msg.ranges[i] < 6 and msg.ranges[i] >.2:
				scanList.append((((float(i)/360.0)*2*math.pi),msg.ranges[i]))

		# iterate through all particles
		for particle in self.particle_cloud:
			angleDif = (particle.theta - self.robot_pose.orientation.z+2*math.pi)%(2*math.pi)
			#iterate through all valid scan points
			errorList = []
			for datum in scanList:
				scanPosition = self.shiftScanToPoint(angleDif, datum, particle)
				#print scanPosition
				pointList.append(scanPosition)
				dist = self.occupancy_field.get_closest_obstacle_distance(scanPosition[0],scanPosition[1])
				errorList.append(math.pow(dist, 3))

			particle.w = 1/(sum(errorList)/len(errorList))
			print "errorAverage: " + str(particle.w)

		#print "normError: " + str(self.particle_cloud)

		# for particle in self.particle_cloud:
		# 	weight = particle.w
		# 	particle.w = 1-weight
			#print "weight: " + str(particle.w)

		weightList = []

		#self.normalize_particles()
		for i in range(len(self.particle_cloud)):
			weightList.append(self.particle_cloud[i].w)
		print weightList

		self.normalize_particles()

		#self.publish_shifted_scan(msg, pointList)


	def shiftScanToPoint(self,angleDif, datum, particle):
		#calculate real world position of datum
		laserAngle = (datum[0]+angleDif + 2*math.pi)%(2*math.pi)
		xDelta = datum[1]*math.cos(laserAngle)
		yDelta = datum[1]*math.sin(laserAngle)
		datumX = xDelta + particle.x
		datumY = yDelta + particle.y
		return (datumX,datumY)

	@staticmethod
	def angle_normalize(z):
		""" convenience function to map an angle to the range [-pi,pi] """
		return math.atan2(math.sin(z), math.cos(z))

	@staticmethod
	def angle_diff(a, b):
		""" Calculates the difference between angle a and angle b (both should be in radians)
			the difference is always based on the closest rotation from angle a to angle b
			examples:
				angle_diff(.1,.2) -> -.1
				angle_diff(.1,2*math.pi-.1) -> .2
				angle_diff(.1,.2+2*math.pi) -> -.1
		"""
		a = ParticleFilter.angle_normalize(a)
		b = ParticleFilter.angle_normalize(b)
		d1 = a-b
		d2 = 2*math.pi - math.fabs(d1)
		if d1 > 0:
			d2 *= -1.0
		if math.fabs(d1) < math.fabs(d2):
			return d1
		else:
			return d2

	@staticmethod
	def weighted_values(values, probabilities, size):
		""" Return a random sample of size elements form the set values with the specified probabilities
			values: the values to sample from (numpy.ndarray)
			probabilities: the probability of selecting each element in values (numpy.ndarray)
			size: the number of samples
		"""
		bins = np.add.accumulate(probabilities)
		return values[np.digitize(random_sample(size), bins)]

	@staticmethod
	def draw_random_sample(choices, probabilities, n):
		""" Return a random sample of n elements from the set choices with the specified probabilities
			choices: the values to sample from represented as a list
			probabilities: the probability of selecting each element in choices represented as a list
			n: the number of samples
		"""
		values = np.array(range(len(choices)))
		probs = np.array(probabilities)
		bins = np.add.accumulate(probs)
		inds = values[np.digitize(random_sample(n), bins)]
		samples = []
		for i in inds:
			samples.append(deepcopy(choices[int(i)]))
		return samples

	def update_initial_pose(self, msg):
		""" Callback function to handle re-initializing the particle filter based on a pose estimate.
			These pose estimates could be generated by another ROS Node or could come from the rviz GUI """
		xy_theta = TransformHelpers.convert_pose_to_xy_and_theta(msg.pose.pose)
		self.initialize_particle_cloud(xy_theta)
		self.fix_map_to_odom_transform(msg)

	def initialize_particle_cloud(self, xy_theta=None):
		""" Initialize the particle cloud.
			Arguments
			xy_theta: a triple consisting of the mean x, y, and theta (yaw) to initialize the
					  particle cloud around.  If this input is ommitted, the odometry will be used """
		
		print "initializing particle cloud"

		self.particle_cloud = []

		unoccupied_cells = self.occupancy_field.unoccupied_cells

		# When no guess given, initialize paricle cloud by random points in known unocupied portion of map
		if xy_theta == None:
			self.particle_cloud = generateRandomParticles(self, self.n_particles)

		else:
			print "guess given"
			for i in range(self.n_particles):
				x = random.gauss(xy_theta[0], 1)
				y = random.gauss(xy_theta[1], 1)
				theta = (random.gauss(xy_theta[2], 1.5))
				rand_particle = Particle(x = x, y = y, theta =  theta)
				self.particle_cloud.append(rand_particle)

		# Get map characteristics to generate points randomly in that realm. Assume
		self.particle_pub.publish()
		self.update_robot_pose()
		print "particle cloud initialized"

	def normalize_particles(self):
		""" Make sure the particle weights define a valid distribution (i.e. sum to 1.0)"""

		numParticles = len(self.particle_cloud)
		weightArray = np.empty([numParticles, 1])
		for i in range(numParticles):
			print 
			weightArray[i] = self.particle_cloud[i].w
		print "weightArray: " + str(weightArray)
		normWeights = weightArray/np.sum(weightArray)
		print "normWeights: " + str(normWeights)
		for i in range(numParticles):
			self.particle_cloud[i].w = normWeights[i][0]

	def publish_predicted_pose(self, msg):
		# actually send the message so that we can view it in rviz
		self.pose_pub.publish(PoseArray(header=Header(stamp=rospy.Time.now(),frame_id=self.map_frame),poses=[self.robot_pose]))

	def publish_particles(self, msg):
		particles_conv = []
		for p in self.particle_cloud:
			particles_conv.append(p.as_pose())
		# actually send the message so that we can view it in rviz
		self.particle_pub.publish(PoseArray(header=Header(stamp=rospy.Time.now(),frame_id=self.map_frame),poses=particles_conv))

	def publish_shifted_scan(self, msg, pointList):
		particles_conv = []
		for point in pointList:
			particles_conv.append(Pose(position=Point(x=point[0],y=point[1],z=0), orientation=Quaternion(x=0, y=0, z=0, w=0)))
		# actually send the message so that we can view it in rviz
		self.scan_shift_pub.publish(PoseArray(header=Header(stamp=rospy.Time.now(),frame_id=self.map_frame),poses=particles_conv))

	def scan_received(self, msg):
		""" This is the default logic for what to do when processing scan data.  Feel free to modify this, however,
			I hope it will provide a good guide.  The input msg is an object of type sensor_msgs/LaserScan """
		#print "scan received"
		if not(self.initialized):
			# wait for initialization to complete
			print "not initialized"
			return

		if not(self.tf_listener.canTransform(self.base_frame,msg.header.frame_id,msg.header.stamp)):
			# need to know how to transform the laser to the base frame
			# this will be given by either Gazebo or neato_node
			return

		if not(self.tf_listener.canTransform(self.base_frame,self.odom_frame,msg.header.stamp)):
			# need to know how to transform between base and odometric frames
			# this will eventually be published by either Gazebo or neato_node
			return

		# calculate pose of laser relative ot the robot base
		p = PoseStamped(header=Header(stamp=rospy.Time(0),frame_id=msg.header.frame_id))
		self.laser_pose = self.tf_listener.transformPose(self.base_frame,p)

		# find out where the robot thinks it is based on its odometry
		p = PoseStamped(header=Header(stamp=msg.header.stamp,frame_id=self.base_frame), pose=Pose())
		self.odom_pose = self.tf_listener.transformPose(self.odom_frame, p)
		# store the the odometry pose in a more convenient format (x,y,theta)
		new_odom_xy_theta = TransformHelpers.convert_pose_to_xy_and_theta(self.odom_pose.pose)

		try:
			self.particle_cloud

		except:
			# now that we have all of the necessary transforms we can update the particle cloud
			self.initialize_particle_cloud()
			# cache the last odometric pose so we can only update our particle filter if we move more than self.d_thresh or self.a_thresh
			self.current_odom_xy_theta = new_odom_xy_theta
			# update our map to odom transform now that the particles are initialized
			self.fix_map_to_odom_transform(msg)

		if (math.fabs(new_odom_xy_theta[0] - self.current_odom_xy_theta[0]) > self.d_thresh or
			  math.fabs(new_odom_xy_theta[1] - self.current_odom_xy_theta[1]) > self.d_thresh or
			  math.fabs(new_odom_xy_theta[2] - self.current_odom_xy_theta[2]) > self.a_thresh):
			# we have moved far enough to do an update!
			self.update_particles_with_odom(msg)	# update based on odometry
			self.update_particles_with_laser(msg)	# update based on laser scan
			self.update_robot_pose()
			self.publish_particles(msg)				# update robot's pose
			self.resample_particles()				# resample particles to focus on areas of high density
			self.fix_map_to_odom_transform(msg)		# update map to odom transform now that we have new particles


		# publish particles (so things like rviz can see them)
		#self.publish_particles(msg)
		self.publish_predicted_pose(msg)

	def fix_map_to_odom_transform(self, msg):
		""" Super tricky code to properly update map to odom transform... do not modify this... Difficulty level infinity. """
		(translation, rotation) = TransformHelpers.convert_pose_inverse_transform(self.robot_pose)
		p = PoseStamped(pose=TransformHelpers.convert_translation_rotation_to_pose(translation,rotation),header=Header(stamp=msg.header.stamp,frame_id=self.base_frame))
		self.odom_to_map = self.tf_listener.transformPose(self.odom_frame, p)
		(self.translation, self.rotation) = TransformHelpers.convert_pose_inverse_transform(self.odom_to_map.pose)

	def broadcast_last_transform(self):
		""" Make sure that we are always broadcasting the last map to odom transformation.
			This is necessary so things like move_base can work properly. """
		if not(hasattr(self,'translation') and hasattr(self,'rotation')):
			return
		self.tf_broadcaster.sendTransform(self.translation, self.rotation, rospy.get_rostime(), self.odom_frame, self.map_frame)

if __name__ == '__main__':
	print "starting"
	n = ParticleFilter()
	r = rospy.Rate(5)

	while not(rospy.is_shutdown()):
		# in the main loop all we do is continuously broadcast the latest map to odom transform
		n.broadcast_last_transform()
		r.sleep()
