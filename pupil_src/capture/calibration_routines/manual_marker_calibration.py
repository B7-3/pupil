'''
(*)~----------------------------------------------------------------------------------
 Pupil - eye tracking platform
 Copyright (C) 2012-2015  Pupil Labs

 Distributed under the terms of the CC BY-NC-SA License.
 License details are in the file license.txt, distributed as part of this software.
----------------------------------------------------------------------------------~(*)
'''

import os
import cv2
import numpy as np
from methods import normalize,denormalize
from gl_utils import draw_gl_point,draw_gl_point_norm,draw_gl_polyline
from circle_detector import get_canditate_ellipses
import calibrate

import audio

from pyglui import ui
from plugin import Calibration_Plugin
from gaze_mappers import Simple_Gaze_Mapper
#logging
import logging
logger = logging.getLogger(__name__)

class Manual_Marker_Calibration(Calibration_Plugin):
    """Detector looks for a white ring on a black background.
        Using at least 9 positions/points within the FOV
        Ref detector will direct one to good positions with audio cues
        Calibration only collects data at the good positions

        Steps:
            Adaptive threshold to obtain robust edge-based image of marker
            Find contours and filter into 2 level list using RETR_CCOMP
            Fit ellipses
    """
    def __init__(self, g_pool,menu_conf = {}):
        super(Manual_Marker_Calibration, self).__init__(g_pool)
        self.active = False
        self.detected = False
        self.pos = None
        self.smooth_pos = 0.,0.
        self.smooth_vel = 0.
        self.sample_site = (-2,-2)
        self.counter = 0
        self.counter_max = 30
        self.candidate_ellipses = []
        self.show_edges = 0
        self.aperture = 7
        self.dist_threshold = 10
        self.area_threshold = 30
        self.world_size = None

        self.stop_marker_found = False
        self.auto_stop = 0
        self.auto_stop_max = 30

        self.menu = None
        self.menu_conf = menu_conf
        self.button = None


    def init_gui(self):
        # atb_label = "calibrate using handheld marker"
        # self._bar.add_var("counter", getter=self.get_count, group="Advanced")
        # self._bar.add_var("area threshold", self.area_threshold, group="Advanced")
        # self._bar.add_var("eccetricity threshold", self.dist_threshold, group="Advanced")
        self.menu = ui.Growing_Menu(self.pretty_class_name)
        self.menu.configuration = self.menu_conf
        self.g_pool.calibration_menu.append(self.menu)

        submenu = ui.Growing_Menu('Advanced')
        submenu.collapsed = True
        self.menu.append(submenu)
        submenu.append(ui.Slider('aperture',self,min=3,step=2,max=11,label='filter aperture'))
        submenu.append(ui.Switch('show_edges',self,label='show edges'))

        self.button = ui.Thumb('active',self,setter=self.toggle,label='Calibrate',hotkey='c')
        self.g_pool.quickbar.append(self.button)

    def deinit_gui(self):
        if self.menu:
            self.g_pool.calibration_menu.remove(self.menu)
            self.menu = None
        if self.button:
            self.g_pool.quickbar.remove(self.button)
            self.button = None


    def toggle(self,new_var):
        if self.active:
            self.stop()
        else:
            self.start()

    def start(self):
        audio.say("Starting Calibration")
        logger.info("Starting Calibration")
        self.active = True
        self.ref_list = []
        self.pupil_list = []


    def stop(self):
        audio.say("Stopping Calibration")
        logger.info("Stopping Calibration")
        self.smooth_pos = 0,0
        self.counter = 0
        self.active = False


        cal_pt_cloud = calibrate.preprocess_data(self.pupil_list,self.ref_list)
        logger.info("Collected %s data points." %len(cal_pt_cloud))
        if len(cal_pt_cloud) < 20:
            logger.warning("Did not collect enough data.")
            return
        cal_pt_cloud = np.array(cal_pt_cloud)
        map_fn = calibrate.get_map_from_cloud(cal_pt_cloud,self.world_size)
        np.save(os.path.join(self.g_pool.user_dir,'cal_pt_cloud.npy'),cal_pt_cloud)

        # prepare destruction of current gaze_mapper... and remove it
        for p in self.g_pool.plugins:
            if p.base_class_name == 'Gaze_Mapping_Plugin':
                p.alive = False
        self.g_pool.plugins = [p for p in g_pool.plugins if p.alive]

        #add new gaze mapper
        self.g_pool.plugins.append(Simple_Gaze_Mapper(self.g_pool,map_fn))
        self.g_pool.plugins.sort(key=lambda p: p.order)


    def get_count(self):
        return self.counter

    def update(self,frame,recent_pupil_positions,events):
        """
        gets called once every frame.
        reference positon need to be published to shared_pos
        if no reference was found, publish 0,0
        """
        if self.active:

            gray_img  = frame.gray

            if self.world_size is None:
                self.world_size = frame.width,frame.height

            self.candidate_ellipses = get_canditate_ellipses(gray_img,
                                                            area_threshold=self.area_threshold,
                                                            dist_threshold=self.dist_threshold,
                                                            min_ring_count=5,
                                                            visual_debug=self.show_edges)

            if len(self.candidate_ellipses) > 0:
                self.detected = True
                marker_pos = self.candidate_ellipses[0][0]
                self.pos = normalize(marker_pos,(frame.width,frame.height),flip_y=True)


            else:
                self.detected = False
                self.pos = None #indicate that no reference is detected


            # center dark or white?
            if self.detected:
                second_ellipse =  self.candidate_ellipses[1]
                col_slice = int(second_ellipse[0][0]-second_ellipse[1][0]/2),int(second_ellipse[0][0]+second_ellipse[1][0]/2)
                row_slice = int(second_ellipse[0][1]-second_ellipse[1][1]/2),int(second_ellipse[0][1]+second_ellipse[1][1]/2)
                marker_gray = gray_img[slice(*row_slice),slice(*col_slice)]
                avg = cv2.mean(marker_gray)
                center = marker_gray[second_ellipse[1][1]/2,second_ellipse[1][0]/2]
                rel_shade = center-avg

                #auto_stop logic
                if rel_shade > 30:
                    #bright marker center found
                    self.auto_stop +=1
                    self.stop_marker_found = True

                else:
                    self.auto_stop = 0
                    self.stop_marker_found = False


            #tracking logic
            if self.detected and not self.stop_marker_found:
                # calculate smoothed manhattan velocity
                smoother = 0.3
                smooth_pos = np.array(self.smooth_pos)
                pos = np.array(self.pos)
                new_smooth_pos = smooth_pos + smoother*(pos-smooth_pos)
                smooth_vel_vec = new_smooth_pos - smooth_pos
                smooth_pos = new_smooth_pos
                self.smooth_pos = list(smooth_pos)
                #manhattan distance for velocity
                new_vel = abs(smooth_vel_vec[0])+abs(smooth_vel_vec[1])
                self.smooth_vel = self.smooth_vel + smoother*(new_vel-self.smooth_vel)

                #distance to last sampled site
                sample_ref_dist = smooth_pos-np.array(self.sample_site)
                sample_ref_dist = abs(sample_ref_dist[0])+abs(sample_ref_dist[1])

                # start counter if ref is resting in place and not at last sample site
                if not self.counter:
                    if self.smooth_vel < 0.01 and sample_ref_dist > 0.1:
                        self.sample_site = self.smooth_pos
                        audio.beep()
                        logger.debug("Steady marker found. Starting to sample %s datapoints" %self.counter_max)
                        self.counter = self.counter_max

                if self.counter:
                    if self.smooth_vel > 0.01:
                        audio.tink()
                        logger.debug("Marker moved to quickly: Aborted sample. Sampled %s datapoints. Looking for steady marker again."%(self.counter_max-self.counter))
                        self.counter = 0
                    else:
                        self.counter -= 1
                        ref = {}
                        ref["norm_pos"] = self.pos
                        ref["timestamp"] = frame.timestamp
                        self.ref_list.append(ref)
                        if self.counter == 0:
                            #last sample before counter done and moving on
                            audio.tink()
                            logger.debug("Sampled %s datapoints. Stopping to sample. Looking for steady marker again."%self.counter_max)


            #always save pupil positions
            for p_pt in recent_pupil_positions:
                if p_pt['confidence'] > self.g_pool.pupil_confidence_threshold:
                    self.pupil_list.append(p_pt)



            #stop if autostop condition is satisfied:
            if self.auto_stop >=self.auto_stop_max:
                self.auto_stop = 0
                self.stop()


        else:
            pass


    def get_init_dict(self):
        if self.menu:
            return {'menu_conf':self.menu.configuration}
        else:
            return {'menu_conf':self.menu_conf}

    def gl_display(self):
        """
        use gl calls to render
        at least:
            the published position of the reference
        better:
            show the detected postion even if not published
        """

        if self.active:
            draw_gl_point_norm(self.smooth_pos,size=15,color=(1.,1.,0.,.5))

        if self.active and self.detected:
            for e in self.candidate_ellipses:
                pts = cv2.ellipse2Poly( (int(e[0][0]),int(e[0][1])),
                                    (int(e[1][0]/2),int(e[1][1]/2)),
                                    int(e[-1]),0,360,15)
                draw_gl_polyline(pts,(0.,1.,0,1.))

            if self.counter:
                # lets draw an indicator on the count
                e = self.candidate_ellipses[2]
                pts = cv2.ellipse2Poly( (int(e[0][0]),int(e[0][1])),
                                    (int(e[1][0]/2),int(e[1][1]/2)),
                                    int(e[-1]),0,360,360/self.counter_max)
                indicator = [e[0]] + pts[self.counter:].tolist()[::-1] + [e[0]]
                draw_gl_polyline(indicator,(0.1,.5,.7,.8),type='Polygon')

            if self.auto_stop:
                # lets draw an indicator on the autostop count
                e = self.candidate_ellipses[2]
                pts = cv2.ellipse2Poly( (int(e[0][0]),int(e[0][1])),
                                    (int(e[1][0]/2),int(e[1][1]/2)),
                                    int(e[-1]),0,360,360/self.auto_stop_max)
                indicator = [e[0]] + pts[self.auto_stop:].tolist() + [e[0]]
                draw_gl_polyline(indicator,(8.,0.1,0.1,.8),type='Polygon')
        else:
            pass

    def cleanup(self):
        """gets called when the plugin get terminated.
        This happends either volunatily or forced.
        if you have an atb bar or glfw window destroy it here.
        """
        if self.active:
            self.stop()
        self.deinit_gui()
