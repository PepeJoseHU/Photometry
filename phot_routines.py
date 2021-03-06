from	astroquery.vizier import Vizier
from	astropy import coordinates as coord
from	astropy import stats, table, time
from	astropy import units as u
from	astropy.io import ascii, fits
import	cat_tools
from	cat_tools import catalog_prop
import	fits_tools
import	glob
from	matplotlib import pylab as plt
from	matplotlib.colors import LogNorm
import	matplotlib.gridspec as gridspec
import	matplotlib.patheffects as PathEffects
from	misc import bcolors
import	numpy as np
import	os
import	photutils
from	plotsettings import *
import	pysynphot as pyS
import	random
from	scipy import stats as stats_scipy
from	scipy import optimize
import	sewpy
import	stat_tools
import	sys
import	warnings

warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)

def aperture_photometry(IMAGE, POSITIONS, RADII, INNERANNULUS, OUTERANNULUS, RMS, GAIN=1, FA=1, ZEROPOINT=0):

	"""
	Performs aperture photometry one or more objects and for one or more circular apertures per object.
	Output: mag in AB and FNU in microJy
	"""

	src_apers											= [photutils.CircularAperture(POSITIONS, r=radius) for radius in RADII]
	src_phot_table										= photutils.aperture_photometry(IMAGE, src_apers)

	# background

	bkg_apers											= [photutils.CircularAnnulus(POSITIONS, r_in=INNERANNULUS[i], r_out=OUTERANNULUS[i]) for i in range(len(RADII))]
	bkg_phot_table										= photutils.aperture_photometry(IMAGE, bkg_apers)

	if 'aperture_sum_0' not in src_phot_table.keys():
		src_phot_table.rename_column('aperture_sum', 'aperture_sum_0')
		bkg_phot_table.rename_column('aperture_sum', 'aperture_sum_0')

	# Get statistics of local background

	local_rms											= [background_local(IMAGE, x)[-1] for x in bkg_apers]	
	# Area ratio

	area_ratio											= [src_apers[i].area() / bkg_apers[i].area() for i in range(len(RADII))]
	bkg_phot_table_rescaled								= [bkg_phot_table['aperture_sum_' + str(i)] * area_ratio[i] for i in range(len(RADII))]

	# sources - background

	for i in range(len(RADII)):
		src_phot_table['bkg_'+str(i)]					= bkg_phot_table_rescaled[i]
		src_phot_table['bkg_sub_aperture_sum_'+str(i)]	= src_phot_table['aperture_sum_'+str(i)] - bkg_phot_table_rescaled[i]

	# Error estimation
	# Comprises of source noise and the scatter in the background
	# noise in drizzled images is correlated

	# Source/Shot/Poisson noise ( ~sqrt(F/effective gain) )
	# Background noise ( ~sqrt(rms**2 * area) )

	for i in range(len(RADII)):

		# Append zeropoint to table

		src_phot_table['ZP_APER_' + str(i)]				= ZEROPOINT[i]

		# Noise terms

		shot_noise										= np.sqrt(np.abs(src_phot_table['bkg_sub_aperture_sum_' + str(i)]) / GAIN)
		# Global background
		#bkg_noise										= np.sqrt(RMS**2 * src_apers[i].area() / FA)

		# Local background
		bkg_noise										= np.sqrt(local_rms[i]**2 * src_apers[i].area() / FA)

		src_phot_table['SHOT_NOISE_' + str(i)]			= shot_noise
		src_phot_table['BKG_NOISE_' + str(i)]			= bkg_noise
		src_phot_table['TOTAL_ERROR_' + str(i)]			= np.sqrt(shot_noise**2 + bkg_noise**2)

		# Flux densities
		# Convert to microJy
		# Converting the ZP for the AB magnitude to a ZP in micro-Jy

		factor											= pow(10, -0.4 * (ZEROPOINT[i] - 23.9))

		src_phot_table['FNU_APER_' + str(i)]			= src_phot_table['bkg_sub_aperture_sum_' + str(i)] * factor
		src_phot_table['FNUERR_APER_' + str(i)]			= src_phot_table['TOTAL_ERROR_' + str(i)] * factor

		# Magnitudes (AB)

		src_phot_table['MAG_APER_' + str(i)]			= [
														-2.5*np.log10(src_phot_table['FNU_APER_' + str(i)][j])
														if src_phot_table['FNU_APER_' + str(i)][j] > 0 else -2.5*np.log10(3 * src_phot_table['FNUERR_APER_' + str(i)][j])
														for j in range(len(src_phot_table['FNU_APER_' + str(i)]))
														]

		src_phot_table['MAGERRP_APER_' + str(i)]		= [
														-2.5*np.log10(src_phot_table['FNU_APER_' + str(i)][j] - src_phot_table['FNUERR_APER_' + str(i)][j]) + 2.5*np.log10(src_phot_table['FNU_APER_' + str(i)][j])
														if src_phot_table['FNU_APER_' + str(i)][j] > 0 else np.nan
														for j in range(len(src_phot_table['FNU_APER_' + str(i)]))
														]

		src_phot_table['MAGERRM_APER_' + str(i)]		= [
														-2.5*np.log10(src_phot_table['FNU_APER_' + str(i)][j]) + 2.5*np.log10(src_phot_table['FNU_APER_' + str(i)][j] + src_phot_table['FNUERR_APER_' + str(i)][j])
														if src_phot_table['FNU_APER_' + str(i)][j] > 0 else np.nan
														for j in range(len(src_phot_table['FNU_APER_' + str(i)]))
														]

		# Transform the magnitudes back to the AB system

		src_phot_table['MAG_APER_' + str(i)]			+= 23.9

	for key in [x for x in src_phot_table.keys() if x or 'MAG_APER_' in x or 'aperture' in x]:
		src_phot_table[key].info.format 				= '%.3f'

	for key in [x for x in src_phot_table.keys() if 'FNU' in x or 'NOISE' in x or 'TOTAL' in x]:
		src_phot_table[key].info.format 				= '%.3e'
	
	return src_phot_table

def	background (IMAGE, DILATE_SIZE=11, NPIXEL=5, SIGMA=5, SNR=5):

	"""
	Extracts the global RMS of an image
	"""

	mask				= photutils.make_source_mask(IMAGE, snr=SNR, npixels=NPIXEL, dilate_size=DILATE_SIZE)
	mean, median, std	= stats.sigma_clipped_stats(IMAGE, sigma=SIGMA, mask=mask)

	return std

def background_local(IMAGE, APERTURE):

	"""
	Extracts the global RMS of an image
	"""

	bkg_mask	= APERTURE.to_mask(method='center')[0]
	bkg_data	= bkg_mask.multiply(IMAGE)
	mask_idx 	= bkg_mask.data > 0
	aper_data 	= bkg_data[mask_idx]

	# Extract the statistics

	mean, median, std = stats.sigma_clipped_stats(aper_data)

	return mean, median, std

def	get_gain(FITS, KEYWORD, LOGGER):

	"""
	Extract the gain value from the header (unit: e-/ADU)
	"""


	if KEYWORD 		== None:
		return 1
	else:
		try:
			hdu		= fits.open(FITS)
			if len(hdu) == 1:
				header = hdu[0].header
			elif len(hdu) > 1:
				header = hdu[0].header + hdu[1].header
			elif len(hdu) > 2:
				header = hdu[0].header + hdu[1].header + hdu[2].header

			if 'CCDGAIN' in header.keys():
				key_gain			= 'CCDGAIN'
			elif 'ADCGAIN' in header.keys():
				key_gain			= 'ADCGAIN'
			elif 'ATODGAIN' in header.keys():
				key_gain			= 'ATODGAIN'

			return header[key_gain]

		except:
			msg		= 'GAIN keyword ({gain}) not found'.format(gain=KEYWORD)
			print(bcolors.BOLD + bcolors.FAIL + '\n{}\n'.format(msg) + bcolors.ENDC)
			LOGGER.error(msg)
			sys.exit()

def hst_aperture_photometry(FITS, POSITIONS, RADII, INNERANNULUS, OUTERANNULUS, PIX2ARCSEC, RMS, FA=1):

	"""
	Wrapper to perfrom aperture photometry on HST images
	"""

	# Read FITS file

	hdulist					= fits.open(FITS)

	hdu_header				= hdulist[0].header
	if len(hdulist) 		> 1:
		try: 
			hdulist[1].shape
			hdu_header		+= hdulist[1].header
			hdu_data		= hdulist[1].data
		except:
			hdu_header		+= hdulist[1].header
			hdu_data		= hdulist[0].data
	else:
		hdu_data			= hdulist[0].data

	# sources

	radii_px				= RADII / PIX2ARCSEC
	innerannulus_px			= INNERANNULUS / PIX2ARCSEC
	outerannulus_px			= OUTERANNULUS / PIX2ARCSEC

	# Zeropoint

	zeropoint				= hst_zeropoint (hdu_header, 2*RADII)

	# Effective gain
	# Astrodrizzled images are in units electrons/s
	# https://photutils.readthedocs.io/en/stable/api/photutils.utils.calc_total_error.html#photutils.utils.calc_total_error

	if 'CCDGAIN' in hdu_header.keys():
		key_gain			= 'CCDGAIN'
	elif 'ADCGAIN' in hdu_header.keys():
		key_gain			= 'ADCGAIN'
	elif 'ATODGAIN' in hdu_header.keys():
		key_gain			= 'ATODGAIN'
	else:
		msg					= 'GAIN keyword not found.'
		logger.error(msg)
		print(bcolors.BOLD + bcolors.FAIL + msg + bcolors.ENDC)
		sys.exit()

	effective_gain			= hdu_header['EXPTIME'] * hdu_header[key_gain]

	# Correlated noise correction factor
	# http://www.ifa.hawaii.edu/~rgal/science/sextractor_notes.html

	pixfrac					= hdu_header['D001PIXF']
	try:
		native_scale		= hst_scale(hdu_header['INSTRUME'], hdu_header['APERTURE'])
	except:
		native_scale		= hst_scale(hdu_header['INSTRUME'], None)

	scale					= PIX2ARCSEC/native_scale

	fa						= pow( (scale/pixfrac) * (1.  - (scale / 3 / pixfrac)), 2) if scale < pixfrac else pow(1 - pixfrac / 3 / scale, 2)

	return aperture_photometry(hdu_data, POSITIONS, radii_px, innerannulus_px, outerannulus_px, RMS, GAIN=effective_gain, ZEROPOINT=zeropoint, FA=fa)

def hst_make_cutout(FITS, COORD_OBS, COORD_EXP, RADII, RADII_INNERANNULUS, RADII_OUTERANNULUS, PIX2ARCSEC, OUTDIR):

	"""
	Makes cuts outs of the aperture
	"""

	# Processing fits file

	hdulist			= fits.open(FITS)

	header			= hdulist[0].header

	if len(hdulist) > 1:
		try: 
			hdulist[1].shape
			image	= hdulist[1].data
		except:
			image	= hdulist[0].data
	else:
		image		= hdulist[0].data

	image_shape		= np.shape(image)

	# Plotsettings

	halfwidth		= 50

	xmin			= int(COORD_OBS[0])-halfwidth if int(COORD_OBS[0])-halfwidth >= 0 else 0
	xmax			= int(COORD_OBS[0])+halfwidth if int(COORD_OBS[0])+halfwidth <= image_shape[0] else image_shape[0]

	ymin			= int(COORD_OBS[1])-halfwidth if int(COORD_OBS[1])-halfwidth >= 0 else 0
	ymax			= int(COORD_OBS[1])+halfwidth if int(COORD_OBS[1])+halfwidth <= image_shape[1] else image_shape[1]

	# Truncate image

	image			= image[ymin:ymax, xmin:xmax]

	image_temp		= image.flatten()
	image_temp		= image_temp[~np.isnan(image_temp)]

	vmin			= np.array([np.percentile(image_temp, x) for x in range(50,90)])
	vmin			= vmin[vmin > 0][5]
	vmax			= np.percentile(image.flatten(), 99)

	plt.figure(1, figsize=(np.sqrt(2) * 9,9))
	plt.subplots_adjust(hspace=0.2, wspace=0.3)

	num_apertures	= len(RADII)

	for i in range(num_apertures):

		ax			= plt.subplot(num_apertures/3 if num_apertures%3 == 0 else num_apertures/3 + 1, 3, i+1)
		ax.imshow(image, cmap=plt.cm.binary, interpolation='Nearest', origin='lower', norm=LogNorm(vmin=vmin, vmax=vmax))

		ax.plot(halfwidth, halfwidth, marker='x', mew=5, color=color_green, ms=12)

		if len(COORD_OBS) > 0:
			ax.errorbar( COORD_OBS[0] - int(COORD_EXP[0]) + halfwidth - 1, COORD_OBS[1] - int(COORD_EXP[1]) + halfwidth - 1, mew=5, marker='x', color=vigit_color_1, ms=12)

		circle1		= plt.Circle((halfwidth, halfwidth), RADII[i]/PIX2ARCSEC, ls='solid',  lw=5,  fc='None', ec=vigit_color_1)
		circle2		= plt.Circle((halfwidth, halfwidth), RADII_INNERANNULUS[i]/PIX2ARCSEC, ls='solid', lw=5, fc='None', ec=color_yellow)
		circle3		= plt.Circle((halfwidth, halfwidth), RADII_OUTERANNULUS[i]/PIX2ARCSEC, ls='solid', lw=5, fc='None', ec=color_yellow)

		ax.add_patch(circle1)
		ax.add_patch(circle2)
		ax.add_patch(circle3)

		plt.setp(ax.get_xticklabels(), visible=False)
		plt.setp(ax.get_yticklabels(), visible=False)

		for line in ax.yaxis.get_majorticklines() + ax.xaxis.get_majorticklines():
			line.set_markersize(0)

		ax.set_xlim(0, 2*halfwidth)
		ax.set_ylim(0, 2*halfwidth)

		ax.text(left+0.05, top-0.05, "\\textbf{{Diameter:}} $\\mathbf{{ {value}''}}$".format(value=2*RADII[i]), ha='left', va='top', transform=ax.transAxes, fontsize=legend_size-4, path_effects=[PathEffects.withStroke(linewidth=6, foreground="w")])

	ax.text(right-0.05, bottom+0.125,  "\\textbf{Host centroid}", ha='right', va='bottom',		transform=ax.transAxes, color=color_green, fontsize=legend_size-4, path_effects=[PathEffects.withStroke(linewidth=6, foreground="w")])
	ax.text(right-0.05, bottom+0.05, "\\textbf{Transient position}",   ha='right', va='bottom',	transform=ax.transAxes, color=vigit_color_1,  fontsize=legend_size-4, path_effects=[PathEffects.withStroke(linewidth=6, foreground="w")])

	plt.savefig(OUTDIR + FITS.replace('.fits', '.pdf'), dpi=600)

	return None

def hst_cog(FITS, POSITIONS, INNERANNULUS, OUTERANNULUS, PIX2ARCSEC, RMS, OUTDIR):

	apertures			= np.linspace(0.2, 5, 20)
	innerannulus		= INNERANNULUS * apertures
	outerannulus		= OUTERANNULUS * apertures

	photometry			= hst_aperture_photometry(FITS, POSITIONS, apertures / 2., innerannulus / 2., outerannulus / 2., PIX2ARCSEC, RMS)

	mags				= np.array([photometry['MAG_APER_' + str(i)] for i in range(len(apertures))])
	mags_errp			= np.array([photometry['MAGERRP_APER_' + str(i)] for i in range(len(apertures))])
	mags_errm			= np.array([photometry['MAGERRM_APER_' + str(i)] for i in range(len(apertures))])
	mask_det			= np.where(mags_errp != 0.)[0]
	mask_ul				= np.where(mags_errp == 0.)[0]

	output				= []
	i					= 0

	mags_det			= mags[mask_det]
	mags_errp_det		= mags_errp[mask_det]
	mags_errm_det		= mags_errm[mask_det]

	cog_data			= table.Table([apertures, [np.round(x[0], 3) for x in mags], [np.round(x[0], 3) for x in mags_errp], [np.round(x[0], 3) for x in mags_errm]], names=('DIAMETER', 'MAG', 'MAGERRP', 'MAGERRM'))
	ascii.write(cog_data, OUTDIR + FITS.replace('.fits', '_cog_data.ascii'), overwrite=True, format='no_header')
	
	# MonteCarlo

	values				= []

	niter				= 10000

	for i in range(len(mags_det)):

		u 				= np.random.uniform(size = niter)
		v 				= np.where(u < 0.5, stats_scipy.norm.ppf(u, mags_det[i], mags_errm_det[i]),
							stats_scipy.norm.ppf(u, mags_det[i], mags_errp_det[i]))

		values.append(v)

	values				= np.array(values)

	cog_stats			= [stats.sigma_clipped_stats(values[:,i])[1] for i in range(values.shape[1])]

	cog_stats_table		= table.Table(np.array([np.mean(cog_stats), np.median(cog_stats), np.std(cog_stats), niter]), names=('MEAN', 'MEDIAN', 'STD', 'NITER'))
	ascii.write(cog_stats_table, OUTDIR + FITS.replace('.fits', '_cog_stat.ascii'), overwrite=True)

	# Plot

	plt.figure(2)
	ax					= plt.subplot(111)

	ax.axhspan(np.median(cog_stats) - 3*np.std(cog_stats), np.median(cog_stats) + 3*np.std(cog_stats), color='0.95')
	ax.axhspan(np.median(cog_stats) - 2*np.std(cog_stats), np.median(cog_stats) + 2*np.std(cog_stats), color='0.85')
	ax.axhspan(np.median(cog_stats) - np.std(cog_stats), np.median(cog_stats) + np.std(cog_stats), color='0.75')
	ax.axhline(np.median(cog_stats), lw=3, color='0.75')

	ax.errorbar(apertures, mags, ms=0, lw=3, color=vigit_color_12)
	ax.errorbar(apertures[mask_det], mags[mask_det], [mags_errp[mask_det], mags_errm[mask_det]], marker='o', ms=9, lw=0, color=vigit_color_12, capsize=0, elinewidth=2)
	ax.errorbar(apertures[mask_ul], mags[mask_ul], marker='v', ms=9, lw=0, color=vigit_color_12, mec=vigit_color_12)

	ax.text(left+0.05, top-0.05, 'median = {median:.3f}, std = {std:.3f}'.format(median=np.median(cog_stats), std=np.std(cog_stats)), ha='left', va='top', fontsize=label_size, transform=ax.transAxes, color='k')

	ax.set_xlabel("Diameter (arcsec)")
	ax.set_ylabel('Brightness (mag, AB)')

	ax.set_xlim(0, apertures[-1])
	ax.set_ylim(max([photometry['MAG_APER_' + str(i)] for i in range(len(apertures))]), min([photometry['MAG_APER_' + str(i)] for i in range(len(apertures))]) - 0.5)

	plt.savefig(OUTDIR + FITS.replace('.fits', '_cog.pdf'))
	
	return None

def hst_scale(INSTRUMENT, MODE):

	"""
	Library of the HST pixel sizes before drizzling.
	"""

	if INSTRUMENT 	== 'NICMOS':
		# http://www.stsci.edu/hst/stis/design/detectors/
		return 0.05071

	if INSTRUMENT 	== 'ACS':
		# http://www.stsci.edu/hst/acs/documents/handbooks/cycle19/c03_intro_acs6.html
		if 'WFC' in MODE:
			return 0.05
		elif MODE 	== 'HRC':
			return 0.0265
		elif MODE 	== 'SBC':
			return 0.032
		else:
			print(bcolors.FAIL + 'MODE {mode} not recognised for {instrument}'.format(mode=MODE, instrument=INSTRUMENT) + bcolors.ENDC)
			sys.exit()

	elif INSTRUMENT 	== 'WFPC2':
		return 0.10

	elif INSTRUMENT == 'WFC3':
		# http://www.stsci.edu/hst/wfc3/documents/handbooks/currentDHB/wfc3_dhb.pdf
		if 'UVIS' in MODE:
			return 0.040
		if MODE 	== 'IR':
			return 0.13
		else:
			print(bcolors.FAIL + 'MODE {mode} not recognised for {instrument}'.format(mode=MODE, instrument=INSTRUMENT) + bcolors.ENDC)
			sys.exit()

def hst_zeropoint (HEADER, DIAMETER):

	"""
	Extracts the zeropoint from the fits header and computes the aperture correction with PySynphot.
	"""

	zeropoints				= -2.5 * np.log10( HEADER['PHOTFLAM'] ) - 5 * np.log10(HEADER['PHOTPLAM']) - 2.408

	# Compute aperture correction
	# Needed if the extraction aperture < 4'' (for larger radii pysynphot crashes. HST considers 5'' as infinite)
	# A spectrum needs to be assumed to compute the aperture correction
	# Varying the temperature from 1000 to 40000 K, alters the AP correction by <~1%

	# mag = -2.5 * log10(F / correction_inf) + ZP
	#     = -2.5 * log10(F) + ZP - 2.5 * log10(correction_inf) 
	# correction_inf == flux ratio between an aperture with a given finite diameter and an aperture with an infinite diameter

	spec_bb					= pyS.BlackBody(10000)

	try:
		filter				= HEADER['FILTER1'] if 'CLEAR' in HEADER['FILTER2'] else HEADER['FILTER2']
	except:
		try:
			filter				= HEADER['FILTER']
		except:
			filter				= HEADER['FILTNAM1']

	DIAMETER				= [x if x < 4 else 4 for x in DIAMETER]

	if HEADER['INSTRUME'].upper() not in ['WFPC2', 'NICMOS']:

		try:
			bandprops			= [pyS.ObsBandpass('{photmode},aper#{aperture:.2f}'.format(
													aperture=x,
													photmode=HEADER['PHOTMODE'].replace(' ', ','),
													))
													for x in DIAMETER]

			bandprops_ref		= pyS.ObsBandpass('{photmode},aper#{aperture:.2f}'.format(
													aperture=4.0,
													photmode=HEADER['PHOTMODE'].replace(' ', ',')
													))
		except:
			bandprops			= [pyS.ObsBandpass('{instrument},{detector},{filter},mjd#{mjd},aper#{aperture:.2f}'.format(
													aperture=x,
													detector=HEADER['APERTURE'],
													filter=filter,
													instrument=HEADER['INSTRUME'],
													mjd=int(time.Time(HEADER['DATE-OBS'], format='isot', scale='utc').jd)
													))
													for x in DIAMETER]

			bandprops_ref		= pyS.ObsBandpass('{instrument},{detector},{filter},mjd#{mjd},aper#{aperture:.2f}'.format(
													aperture=4.0,
													detector=HEADER['APERTURE'],
													filter=filter,
													instrument=HEADER['INSTRUME'],
													mjd=int(time.Time(HEADER['DATE-OBS'], format='isot', scale='utc').jd)
													))

		ap_correction			= np.ones(len(DIAMETER))*1.0

		for i in range(len(DIAMETER)):
			if DIAMETER[i] < 4.:
				ap_correction[i]= pyS.Observation(spec_bb, bandprops[i]).countrate() / pyS.Observation(spec_bb, bandprops_ref).countrate()
			else:
				ap_correction[i]= 1

	elif HEADER['INSTRUME'].upper() in ['WFPC2', 'NICMOS']:

		print (bcolors.WARNING + 'Aperture correction of the {} camera is not tabulated in the pySynphot.'.format(HEADER['INSTRUME'].upper()))
		print (                  'The AP correction is -0.1 mag for a circulat aperture with a radius of 0.5".')
		print (                  'This value needs to be added by hand to the photometry!' + bcolors.ENDC)

		ap_correction			= np.ones(len(DIAMETER))*1.0

	# elif HEADER['INSTRUME'].upper() == 'WFPC2':

	# 	print (bcolors.WARNING + 'Aperture correction of the WFPC2 camera is not tabulated in the pySynphot.')
	# 	print (                  'The AP correction is -0.1 mag for a circulat aperture with a radius of 0.5".')
	# 	print (                  'This value needs to be added by hand to the photometry!' + bcolors.ENDC)

	# 	ap_correction			= 1



	ap_correction_mag		= -2.5 * np.log10(ap_correction)

	return zeropoints - ap_correction_mag

def local_sequence(CAT, AUTO=False, FILENAME=None, FITS='', LOGGER=None, LOWER=10, PATH='', UPPER=90):

	"""
	Select stars for local sequence
	"""

	if FILENAME				== None:

		print(bcolors.FAIL + 'Filename of the local sequence not specified' + bcolors.ENDC)
		sys.exit()

	# Image cuts

	instrument_settings		= table.Table(names=('TELESCOPE', 'FILTER', 'MAG_BRIGHT', 'MAG_FAINT'), dtype=('S100', 'S100', 'f', 'f'))
	instrument_settings.add_row(['PanSTARRS', 'g', -18, -13])
	instrument_settings.add_row(['PanSTARRS', 'r', -18, -13])
	instrument_settings.add_row(['PanSTARRS', 'i', -18, -13])
	instrument_settings.add_row(['PanSTARRS', 'z', -18, -12])
	instrument_settings.add_row(['PanSTARRS', 'y', -18, -12])
	instrument_settings.add_row(['2MASS', 'J', -10, -6])
	instrument_settings.add_row(['2MASS', 'H', -10, -6])
	instrument_settings.add_row(['2MASS', 'K', -10, -6.5])
	instrument_settings.add_row(['UKIDSS', 'J', -16, -11])
	instrument_settings.add_row(['UKIDSS', 'H', -16, -11])
	instrument_settings.add_row(['UKIDSS', 'K', -16, -11])
	instrument_settings.add_row(['UKIDSS', 'Y', -16, -8])
	instrument_settings.add_row(['SDSS', 'u', -7, -3])
	instrument_settings.add_row(['SDSS', 'g', -10, -3])
	instrument_settings.add_row(['SDSS', 'r', -10, -3])
	instrument_settings.add_row(['SDSS', 'i', -10, -4])
	instrument_settings.add_row(['SDSS', 'z', -10, -4])

	if AUTO:

		instrument_telescope= FITS.split('_')[1]
		instrument_filter	= [x for x in FITS.replace('.fits', '').split('_') if len(x) == 1][0]

		auto_mag_bright		= instrument_settings['MAG_BRIGHT'][(instrument_telescope == instrument_settings['TELESCOPE']) & (instrument_filter == instrument_settings['FILTER'])][0]
		auto_mag_faint		= instrument_settings['MAG_FAINT'] [(instrument_telescope == instrument_settings['TELESCOPE']) & (instrument_filter == instrument_settings['FILTER'])][0]

		print(bcolors.OKGREEN + 'Lower: ' + str(np.round(auto_mag_bright, 2)) + bcolors.ENDC)
		print(bcolors.OKGREEN + 'Upper: ' + str(np.round(auto_mag_faint,  2)) + bcolors.ENDC)

		mask_good			= np.where((auto_mag_bright <= CAT['MAG_INS']) & (CAT['MAG_INS'] <= auto_mag_faint))[0]

		LOGGER.info('Magnitude range: {magbright:.2f} - {magfaint:.2f}'.format(magbright=auto_mag_bright, magfaint=auto_mag_faint))

		print(bcolors.OKGREEN + 'Number of stars: ' + str(len(mask_good)) + '\n' + bcolors.ENDC)

		data_x				= CAT['MAG_INS'][mask_good]
		data_y				= CAT['MAG_CAT'][mask_good]
		data_y_err			= CAT['MAGERR_CAT'][mask_good]

		if all(CAT['MAGERR_CAT']) != 0:
			pinit			= 0
			fitfunc			= lambda p, x: p + x
			errfunc			= lambda p, x, y, err: (y - fitfunc(p, x)) / err
			out				= optimize.leastsq(errfunc, pinit,args=(data_x, data_y, data_y_err), full_output=True)

		else:
			pinit			= 0
			fitfunc			= lambda p, x: p + x
			errfunc			= lambda p, x, y: (y - fitfunc(p, x))
			out				= optimize.leastsq(errfunc, pinit,args=(data_x, data_y), full_output=True)

		pfinal				= out[0]
		cont_direct			= pfinal[0]

		# Diagnostic plot: instrumental vs. apparent magnitude

		print(bcolors.OKGREEN + '\nGenerate diagnostic plot to remove stars' + bcolors.ENDC)

		plt.figure(2, figsize=(9*np.sqrt(2.),9))

		loc_ax				= plt.subplot(111)
		loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=20, color=vigit_color_12, alpha=0.25, zorder=0)
		loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=2, color=vigit_color_12, zorder=1)

		loc_ax.errorbar(CAT['MAG_INS'], CAT['MAG_CAT'], CAT['MAGERR_CAT'], CAT['MAGERR_INS'], lw=0, ms=10, marker='o', color='0.75', elinewidth=2, capsize=0, zorder=2)
		loc_ax.errorbar(CAT['MAG_INS'][mask_good], CAT['MAG_CAT'][mask_good], CAT['MAGERR_CAT'][mask_good], CAT['MAGERR_INS'][mask_good], lw=0, ms=10, marker='o', color='k', elinewidth=2, capsize=0, zorder=3)

		loc_ax.axvline(auto_mag_bright, color='k', ls='--', zorder=4)
		loc_ax.axvline(auto_mag_faint, color='k', ls='--', zorder=4)

		loc_ax.set_xlabel("Instrumental magnitude (mag)")
		loc_ax.set_ylabel("Apparent magnitude (mag)")

		if len(CAT['MAG_CAT']) > 1:
			loc_ax.set_xlim(min(CAT['MAG_INS'])-0.2, min(max(CAT['MAG_INS']), 0)+0.2)
			loc_ax.set_ylim(min(CAT['MAG_CAT'])-0.2, max(CAT['MAG_CAT'])+0.2)
		else:
			loc_ax.set_xlim(CAT['MAG_INS']-0.2, CAT['MAG_INS']+0.2)
			loc_ax.set_ylim(CAT['MAG_CAT']-0.2, CAT['MAG_CAT']+0.2)

		loc_ax.grid(True)
		plt.savefig(PATH+FITS.replace('.fits', '_std.pdf'), dpi=600)
		plt.close()

		ascii.write(np.array([CAT['XWIN_IMAGE'][mask_good], CAT['YWIN_IMAGE'][mask_good],
			CAT['ALPHAWIN_J2000'][mask_good], CAT['DELTAWIN_J2000'][mask_good],
			CAT['MAG_CAT'][mask_good], CAT['MAGERR_CAT'][mask_good]]).T,
			FILENAME,
			names=['XWIN_IMAGE', 'YWIN_IMAGE', 'ALPHAWIN_J2000', 'DELTAWIN_J2000', 'MAG', 'MAG_ERR'], overwrite=True)

	elif (LOWER == 0. and UPPER == 100):

		data_x				= CAT['MAG_INS']
		data_y				= CAT['MAG_CAT']
		data_y_err			= CAT['MAGERR_CAT']

		if all(CAT['MAGERR_CAT']) != 0:
			pinit			= 0
			fitfunc			= lambda p, x: p + x
			errfunc			= lambda p, x, y, err: (y - fitfunc(p, x)) / err
			out				= optimize.leastsq(errfunc, pinit,args=(data_x, data_y, data_y_err), full_output=True)

		else:
			pinit			= 0
			fitfunc			= lambda p, x: p + x
			errfunc			= lambda p, x, y: (y - fitfunc(p, x))
			out				= optimize.leastsq(errfunc, pinit,args=(data_x, data_y), full_output=True)

		pfinal				= out[0]
		cont_direct			= pfinal[0]

		# Diagnostic plot: instrumental vs. apparent magnitude

		print(bcolors.OKGREEN + '\nGenerate diagnostic plot to remove stars' + bcolors.ENDC)

		plt.figure(2, figsize=(9*np.sqrt(2.),9))

		loc_ax				= plt.subplot(111)
		loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=20, color=vigit_color_12, alpha=0.25, zorder=0)
		loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=2, color=vigit_color_12, zorder=1)

		loc_ax.errorbar(CAT['MAG_INS'], CAT['MAG_CAT'], CAT['MAGERR_CAT'], CAT['MAGERR_INS'], lw=0, ms=10, marker='o', color='k', elinewidth=2, capsize=0, zorder=3)

		loc_ax.set_xlabel("Instrumental magnitude (mag)")
		loc_ax.set_ylabel("Apparent magnitude (mag)")

		if len(CAT['MAG_CAT']) > 1:
			loc_ax.set_xlim(min(CAT['MAG_INS'])-0.2, min(max(CAT['MAG_INS']), 0)+0.2)
			loc_ax.set_ylim(min(CAT['MAG_CAT'])-0.2, max(CAT['MAG_CAT'])+0.2)
		else:
			loc_ax.set_xlim(CAT['MAG_INS']-0.2, CAT['MAG_INS']+0.2)
			loc_ax.set_ylim(CAT['MAG_CAT']-0.2, CAT['MAG_CAT']+0.2)

		loc_ax.grid(True)
		plt.savefig(PATH+FITS.replace('.fits', '_std.pdf'), dpi=600)
		plt.close()

		ascii.write(np.array([CAT['XWIN_IMAGE'], CAT['YWIN_IMAGE'],
			CAT['ALPHAWIN_J2000'], CAT['DELTAWIN_J2000'],
			CAT['MAG_CAT'], CAT['MAGERR_CAT']]).T,
			FILENAME,
			names=['XWIN_IMAGE', 'YWIN_IMAGE', 'ALPHAWIN_J2000', 'DELTAWIN_J2000', 'MAG', 'MAG_ERR'], overwrite=True)

	else:
		# Manual

		if len(CAT['MAG_INS']) > 10:
			mask_good		= np.where((np.percentile(CAT['MAG_INS'], LOWER) <= CAT['MAG_INS']) & (CAT['MAG_INS'] <= np.percentile(CAT['MAG_INS'], UPPER)))[0]
		else:
			mask_good		= np.where((np.percentile(CAT['MAG_INS'], 0)  <= CAT['MAG_INS']) & (CAT['MAG_INS'] <= np.percentile(CAT['MAG_INS'], 100)))[0]

		data_x				= CAT['MAG_INS'][mask_good]
		data_y				= CAT['MAG_CAT'][mask_good]
		data_y_err			= CAT['MAGERR_CAT'][mask_good]

		if all(CAT['MAGERR_CAT']) != 0:
			pinit			= 0
			fitfunc			= lambda p, x: p + x
			errfunc			= lambda p, x, y, err: (y - fitfunc(p, x)) / err
			out				= optimize.leastsq(errfunc, pinit,args=(data_x, data_y, data_y_err), full_output=True)

		else:
			pinit			= 0
			fitfunc			= lambda p, x: p + x
			errfunc			= lambda p, x, y: (y - fitfunc(p, x))
			out				= optimize.leastsq(errfunc, pinit,args=(data_x, data_y), full_output=True)

		pfinal				= out[0]
		cont_direct			= pfinal[0]

		# Diagnostic plot: instrumental vs. apparent magnitude

		print(bcolors.HEADER + '\nGenerate diagnostic plot to remove stars' + bcolors.ENDC)

		plt.figure(2, figsize=(9*np.sqrt(2.),9))

		loc_ax				= plt.subplot(111)
		loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=20, color=vigit_color_12, alpha=0.25, zorder=0)
		loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=2, color=vigit_color_12, zorder=1)

		loc_ax.errorbar(CAT['MAG_INS'], CAT['MAG_CAT'], CAT['MAGERR_CAT'], CAT['MAGERR_INS'], lw=0, ms=10, marker='o', color='0.75', elinewidth=2, capsize=0, zorder=2)
		loc_ax.errorbar(CAT['MAG_INS'][mask_good], CAT['MAG_CAT'][mask_good], CAT['MAGERR_CAT'][mask_good], CAT['MAGERR_INS'][mask_good], lw=0, ms=10, marker='o', color='k', elinewidth=2, capsize=0, zorder=3)

		if len(CAT['MAG_INS']) > 10:
			loc_ax.axvline(np.percentile(CAT['MAG_INS'], LOWER), color='k', ls='--', zorder=4)
			loc_ax.axvline(np.percentile(CAT['MAG_INS'], UPPER), color='k', ls='--', zorder=4)

		loc_ax.set_xlabel("Instrumental magnitude (mag)")
		loc_ax.set_ylabel("Apparent magnitude (mag)")

		if len(CAT['MAG_CAT']) > 1:
			loc_ax.set_xlim(min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2)
			loc_ax.set_ylim(min(CAT['MAG_CAT'])-0.2, max(CAT['MAG_CAT'])+0.2)
		else:
			loc_ax.set_xlim(CAT['MAG_INS']-0.2, CAT['MAG_INS']+0.2)
			loc_ax.set_ylim(CAT['MAG_CAT']-0.2, CAT['MAG_CAT']+0.2)

		loc_ax.grid(True)
		plt.savefig(PATH+FITS.replace('.fits', '_std.pdf'), dpi=600)

		plt.show()
		plt.close()

		# Apply magnitude cuts

		print('Current magnitude cuts:')

		if len(CAT['MAG_INS']) > 10:
			print('Lower: ' + str(np.round(np.percentile(CAT['MAG_INS'], LOWER), 2)))
			print('Upper: ' + str(np.round(np.percentile(CAT['MAG_INS'], UPPER), 2)))
		else:
			print('Lower: not defined')
			print('Upper: not defined')

		if sys.version_info[0] == 2:
			var1			= raw_input(bcolors.BOLD + bcolors.OKBLUE + '\nWould you like to apply a magnitude cut? [y|[n]] ' + bcolors.ENDC)
		elif sys.version_info[0] == 3:
			var1			= input(bcolors.BOLD + bcolors.OKBLUE + '\nWould you like to apply a magnitude cut? [y|[n]] ' + bcolors.ENDC)
		else:
			print(bcolors.FAIL + bcolors.BOLD + 'Programme requires either Python 2 or 3.' + bcolors.ENDC)
			sys.exit()

		flag_loop			= var1

		CAT_CLEANED			= []

		if flag_loop != 'y':
			matched_catalog_cleaned	= CAT[mask_good]
			ascii.write(np.array([matched_catalog_cleaned['XWIN_IMAGE'], matched_catalog_cleaned['YWIN_IMAGE'],
				matched_catalog_cleaned['ALPHAWIN_J2000'], matched_catalog_cleaned['DELTAWIN_J2000'],
				matched_catalog_cleaned['MAG_CAT'], matched_catalog_cleaned['MAGERR_CAT']]).T,
				FILENAME,
				names=['XWIN_IMAGE', 'YWIN_IMAGE', 'ALPHAWIN_J2000', 'DELTAWIN_J2000', 'MAG', 'MAG_ERR'], overwrite=True)

		while(flag_loop is 'y'):

			if sys.version_info[0] == 2:
				mag_cut_bright	= float(raw_input(bcolors.WARNING  + 'Limit for the bright stars: ' + bcolors.ENDC))
				mag_cut_faint	= float(raw_input(bcolors.WARNING  + 'Limit for the faint stars: '  + bcolors.ENDC))
			elif sys.version_info[0] == 3:
				mag_cut_bright	= float(input(bcolors.WARNING  + 'Limit for the bright stars: ' + bcolors.ENDC))
				mag_cut_faint	= float(input(bcolors.WARNING  + 'Limit for the faint stars: '  + bcolors.ENDC))
			else:
				print(bcolors.FAIL + bcolors.BOLD + 'Programme requires either Python 2 or 3.' + bcolors.ENDC)
				sys.exit()

			# Cleaning of the catalog

			CAT_CLEANED		= table.vstack([x for x in CAT if mag_cut_bright <= x['MAG_INS'] <= mag_cut_faint])

			print(bcolors.OKGREEN + '\nNumber of stars: ' + str(len(CAT_CLEANED)) + '\n' + bcolors.ENDC)


			data_x			= CAT_CLEANED['MAG_INS']
			data_y			= CAT_CLEANED['MAG_CAT']
			data_y_err		= CAT_CLEANED['MAGERR_CAT']


			if all(CAT_CLEANED['MAGERR_CAT']) != 0:

				pinit		= 0
				fitfunc 	= lambda p, x: p + x
				errfunc 	= lambda p, x, y, err: (y - fitfunc(p, x)) / err
				out			= optimize.leastsq(errfunc, pinit,args=(data_x, data_y, data_y_err), full_output=True)

			else:

				pinit		= 0
				fitfunc 	= lambda p, x: p + x
				errfunc 	= lambda p, x, y: (y - fitfunc(p, x))
				out			= optimize.leastsq(errfunc, pinit,args=(data_x, data_y), full_output=True)

			pfinal			= out[0]
			cont_direct		= pfinal[0]

			plt.figure(3, figsize=(9*np.sqrt(2.),9))

			loc_ax			= plt.subplot(111)
			loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=20, color=vigit_color_12, alpha=0.5, zorder=0)
			loc_ax.plot(np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), cont_direct + np.array([min(CAT['MAG_INS'])-0.2, max(CAT['MAG_INS'])+0.2]), lw=2, color=vigit_color_12, zorder=1)

			loc_ax.errorbar(CAT['MAG_INS'], CAT['MAG_CAT'], CAT['MAGERR_CAT'], CAT['MAGERR_INS'], lw=0, ms=10, marker='o', color='0.75', elinewidth=2, capsize=0, zorder=2)
			loc_ax.errorbar(CAT_CLEANED['MAG_INS'], CAT_CLEANED['MAG_CAT'], CAT_CLEANED['MAGERR_CAT'], CAT_CLEANED['MAGERR_INS'], lw=0, ms=10, marker='o', color='k', elinewidth=2, capsize=0, zorder=3)

			loc_ax.axvline(mag_cut_bright, color='k', ls='--', zorder=4)
			loc_ax.axvline(mag_cut_faint,  color='k', ls='--', zorder=4)

			loc_ax.set_xlabel("Instrumental magnitude (mag)")
			loc_ax.set_ylabel("Apparent magnitude (mag)")

			if len(CAT['MAG_CAT']) > 1:
				loc_ax.set_xlim(min(CAT_CLEANED['MAG_INS'])-0.2, max(CAT_CLEANED['MAG_INS'])+0.2)
				loc_ax.set_ylim(min(CAT_CLEANED['MAG_CAT'])-0.2, max(CAT_CLEANED['MAG_CAT'])+0.2)
			else:
				loc_ax.set_xlim(CAT_CLEANED['MAG_INS']-0.2, CAT_CLEANED['MAG_INS']+0.2)
				loc_ax.set_ylim(CAT_CLEANED['MAG_CAT']-0.2, CAT_CLEANED['MAG_CAT']+0.2)

			loc_ax.set_xlabel("Instrumental magnitude (mag)")
			loc_ax.set_ylabel("Apparent magnitude (mag)")
			loc_ax.grid(True)

			plt.savefig(PATH+FITS.replace('.fits', '_std.pdf'), dpi=600)

			plt.show()
			plt.close()

			ascii.write(np.array([CAT_CLEANED['XWIN_IMAGE'], CAT_CLEANED['YWIN_IMAGE'],
				CAT_CLEANED['ALPHAWIN_J2000'], CAT_CLEANED['DELTAWIN_J2000'],
				CAT_CLEANED['MAG_CAT'], CAT_CLEANED['MAGERR_CAT']]).T,
				FILENAME,
				names=['XWIN_IMAGE', 'YWIN_IMAGE', 'ALPHAWIN_J2000', 'DELTAWIN_J2000', 'MAG', 'MAG_ERR'], overwrite=True)


			if sys.version_info[0] == 2:
				flag_loop	= raw_input(bcolors.BOLD + bcolors.WARNING + 'Would you like to change the cuts? [y|[n]] ' + bcolors.ENDC)
			elif sys.version_info[0] == 3:
				flag_loop 	= input(bcolors.BOLD + bcolors.WARNING + 'Would you like to change the cuts? [y|[n]] ' + bcolors.ENDC)
			else:
				print(bcolors.FAIL + bcolors.BOLD + 'Programme requires either Python 2 or 3.' + bcolors.ENDC)
				sys.exit()

		if 'auto_mag_bright' in locals():
			LOGGER.info('Magnitude range: {magbright:.2f} - {magfaint:.2f}'.format(magbright=auto_mag_bright, magfaint=auto_mag_faint))
		else:
			if 'mag_cut_bright' in locals():
				LOGGER.info('Magnitude range: {magbright:.2f} - {magfaint:.2f}'.format(magbright=mag_cut_bright, magfaint=mag_cut_faint))
			else:
				LOGGER.info('Magnitude range: {magbright:.2f} - {magfaint:.2f}'.format(magbright=min(CAT['MAG_INS']), magfaint=max(CAT['MAG_INS'])))

		if len(CAT) 		== 0:
			print(bcolors.FAIL + 'There is no star in the cleaned catalog. Check your input parameters.' + bcolors.ENDC)
			sys.exit()

	if 'CAT_CLEANED' in locals():
		if len(CAT_CLEANED) == 0:
			return {'NUMSTARS': len(matched_catalog_cleaned), 'CAT': matched_catalog_cleaned}
		else:
			return {'NUMSTARS': len(CAT_CLEANED), 'CAT': CAT_CLEANED}
	else:
		return {'NUMSTARS': len(CAT), 'CAT': CAT}

def make_poststamp(FITS, COORD_EXP, COORD_OBS, PATH=''):


	# Open images with apertures

	check_hdu		= fits.open('check_'+FITS)

	if len(check_hdu) > 1:

		check_image	= check_hdu[1].data
		check_header= check_hdu[1].header

	else:

		check_image	= check_hdu[0].data
		check_header= check_hdu[0].header


	xmin			= int(COORD_EXP[0])-50 if int(COORD_EXP[0])-50 >= 0 else 0
	xmax			= int(COORD_EXP[0])+50 if int(COORD_EXP[0])+50 <= check_header['NAXIS1'] else check_header['NAXIS1']

	ymin			= int(COORD_EXP[1])-50 if int(COORD_EXP[1])-50 >= 0 else 0
	ymax			= int(COORD_EXP[1])+50 if int(COORD_EXP[1])+50 <= check_header['NAXIS2'] else check_header['NAXIS2']

	check_image	= check_image[ymin:ymax, xmin:xmax]

	# Set brightness cuts

	check_image_temp= check_image.flatten()
	check_image_temp= sorted(check_image_temp[~np.isnan(check_image_temp)])

	check_vmin		= np.array([np.percentile(check_image_temp, x) for x in range(50,99)])
	try:
		check_vmin	= check_vmin[check_vmin > 0][5]
	except:
		check_vmin 	= max(check_image_temp) / 0.5

	check_vmax		= np.percentile(check_image_temp, 97)

	# Open science frame

	sci_hdu			= fits.open(FITS)

	if len(sci_hdu) > 1:

		sci_image	= sci_hdu[1].data
		sci_header	= sci_hdu[1].header

	else:

		sci_image	= sci_hdu[0].data
		sci_header	= sci_hdu[0].header

	xmin			= int(COORD_EXP[0])-50 if int(COORD_EXP[0])-50 >= 0 else 0
	xmax			= int(COORD_EXP[0])+50 if int(COORD_EXP[0])+50 <= sci_header['NAXIS1'] else sci_header['NAXIS1']

	ymin			= int(COORD_EXP[1])-50 if int(COORD_EXP[1])-50 >= 0 else 0
	ymax			= int(COORD_EXP[1])+50 if int(COORD_EXP[1])+50 <= sci_header['NAXIS2'] else sci_header['NAXIS2']

	sci_image		= sci_image[ymin:ymax, xmin:xmax]

	sci_image_temp	= sci_image.flatten()
	sci_image_temp	= sorted(sci_image_temp[~np.isnan(sci_image_temp)])

	sci_vmin		= np.array([np.percentile(sci_image_temp, x) for x in range(50,99)])
	try:
		sci_vmin	= sci_vmin[sci_vmin > 0][1]
	except:
		sci_vmin 	= max(sci_image_temp) / 0.5

	sci_vmax		= np.percentile(sci_image_temp, 99)

	# Making the plot

	plt.figure(4, figsize=(np.sqrt(2)*9, 9))

	post_ax			= plt.subplot(121)
	post_bx			= plt.subplot(122)

	post_ax.imshow(check_image, cmap=plt.cm.binary, interpolation='Nearest', origin='lower', norm=LogNorm(vmin=check_vmin, vmax=check_vmax))
	post_bx.imshow(sci_image,   cmap=plt.cm.binary, interpolation='Nearest', origin='lower', norm=LogNorm(vmin=sci_vmin,   vmax=sci_vmax))

	post_ax.plot(50, 50, marker='x', mew=5, color=vigit_color_1, ms=12)
	post_bx.plot(50, 50, marker='x', mew=5, color=vigit_color_1, ms=12)

	if len(COORD_OBS) > 0:
		post_ax.errorbar( COORD_OBS[0] - int(COORD_EXP[0]) + 50 - 1, COORD_OBS[1] - int(COORD_EXP[1]) + 50 - 1, mew=5, marker='x', color=color_green, ms=12)
		post_bx.errorbar( COORD_OBS[0] - int(COORD_EXP[0]) + 50 - 1, COORD_OBS[1] - int(COORD_EXP[1]) + 50 - 1, mew=5, marker='x', color=color_green, ms=12)

	plt.setp(post_ax.get_xticklabels(), visible=False)
	plt.setp(post_ax.get_yticklabels(), visible=False)

	plt.setp(post_bx.get_xticklabels(), visible=False)
	plt.setp(post_bx.get_yticklabels(), visible=False)

	for line in post_ax.yaxis.get_majorticklines() + post_ax.xaxis.get_majorticklines():
	    line.set_markersize(0)

	for line in post_bx.yaxis.get_majorticklines() + post_bx.xaxis.get_majorticklines():
	    line.set_markersize(0)

	post_bx.text(right-0.05, top-0.05,  "\\textbf{Observed}", ha='right', va='top', transform=post_bx.transAxes, color=color_green, fontsize=legend_size, path_effects=[PathEffects.withStroke(linewidth=6, foreground="w")])
	post_bx.text(right-0.05, top-0.125, "\\textbf{Expected}", ha='right', va='top', transform=post_bx.transAxes, color=color_blue,  fontsize=legend_size, path_effects=[PathEffects.withStroke(linewidth=6, foreground="w")])

	post_ax.set_xlim(0,100)
	post_ax.set_ylim(0,100)

	post_bx.set_xlim(0,100)
	post_bx.set_ylim(0,100)

	plt.savefig(PATH+FITS.replace('.fits', '_poststamp.pdf'), dpi=600)

	return None

def make_scicat (FITS, OBJECT_PROPERTIES, SEXTRACTOR_PHOTOMETRY, PHOTUTILS_PHOTOMETRY, ZEROPOINT_SUMMARY, OFFSET, LOGGER):

	"""
	Generates an output table. Contains information about the observation and the measurements.
	"""

	hdu_header				= fits.getheader(FITS)

	catalog					= table.Table(names=('PROPERTY', 'VALUE', 'ERROR+', 'ERROR-', 'COMMENT'), dtype=('S100', 'f', 'f', 'f', 'S100'))

	# Filename

	catalog.add_row(['FILENAME', np.nan, np.nan, np.nan, FITS])

	# Observing time, exposure time (individual and number of exposures)

	for key in ['DATE-OBS', 'MJD', 'EXPTIME', 'NCOMBINE']:
		try:
			catalog.add_row([key, np.nan, np.nan, np.nan, hdu_header[key]])
		except:
			if key 			== 'NCOMBINE':
				catalog.add_row([key, np.nan, np.nan, np.nan, 1])
			else:
				catalog.add_row([key, np.nan, np.nan, np.nan, '...'])

	if not 'T' in catalog['COMMENT'][catalog['PROPERTY'] == 'DATE-OBS']:
		try:
			time_utc 		= '{date}T{time}'.format(date=hdu_header['ut-date'], time=hdu_header['ut-time'])
			catalog['COMMENT'][catalog['PROPERTY'] == 'DATE-OBS'] 	= time_utc

			time_mjd		= time.Time(catalog['COMMENT'][catalog['PROPERTY'] == 'DATE-OBS'], format='isot', scale='utc').mjd
			catalog['COMMENT'][catalog['PROPERTY'] == 'MJD'] 		= '{:.7f}'.format(time_mjd)

		except:
			try:
				time_utc 	= '{date}T{time}'.format(date=hdu_header['ut-date'.upper()], time=hdu_header['ut-time'.upper()])
				catalog['COMMENT'][catalog['PROPERTY'] == 'DATE-OBS'] 	= time_utc

				time_mjd	= time.Time(catalog['COMMENT'][catalog['PROPERTY'] == 'DATE-OBS'], format='isot', scale='utc').mjd
				catalog['COMMENT'][catalog['PROPERTY'] == 'MJD'] 		= '{:.7f}'.format(time_mjd)

			except:
				pass

	# Calibrated against?

	catalog.add_row(['PHOTCAL', np.nan, np.nan, np.nan, OBJECT_PROPERTIES['PHOTCAL']])

	# Which object did we analyse

	catalog.add_row(['RA', 			'{:.6f}'.format(OBJECT_PROPERTIES['RA'][0]),	np.nan, np.nan, 'degree'])
	catalog.add_row(['DEC', 		'{:.6f}'.format(OBJECT_PROPERTIES['DEC'][0]), 	np.nan, np.nan, 'degree'])

	catalog.add_row(['X_IMAGE_EXP', '{:.1f}'.format(OBJECT_PROPERTIES['X_EXP'][0]), np.nan, np.nan, 'px'])
	catalog.add_row(['Y_IMAGE_EXP', '{:.1f}'.format(OBJECT_PROPERTIES['Y_EXP'][0]), np.nan, np.nan, 'px'])

	# Compute for all objects in SEXTRACTOR PHOTOMETRY the distance to the expected source position (unit: arcsec and pix)
	# Identify the object with the smallest offset as the science object

	if len(SEXTRACTOR_PHOTOMETRY) 	> 0:

		msg					= 'One or more object found within {offset:.1f} arcsec from the source position'.format(offset=OFFSET)
		print(bcolors.OKGREEN + '\n{}\n'.format(msg) + bcolors.ENDC)
		LOGGER.info(msg)

		# If more than one object, take the most nearby one

		distance			= [np.sqrt( (x['XWIN_IMAGE'] - OBJECT_PROPERTIES['X_EXP'])**2 +  (x['YWIN_IMAGE'] - OBJECT_PROPERTIES['Y_EXP'])**2) for x in SEXTRACTOR_PHOTOMETRY]

		SEXTRACTOR_PHOTOMETRY['DISTANCE (px)']	  = [float(x) for x in distance]
		SEXTRACTOR_PHOTOMETRY['DISTANCE (arcsec)'] = SEXTRACTOR_PHOTOMETRY['DISTANCE (px)']*fits_tools.pix2arcsec(FITS)
		SEXTRACTOR_PHOTOMETRY.sort('DISTANCE (px)')

		for key in ['XWIN_IMAGE', 'YWIN_IMAGE']:
			catalog.add_row([key+'_OBS', '{:.1f}'.format(SEXTRACTOR_PHOTOMETRY[key][0]), np.nan, np.nan, 'px'])

		catalog.add_row(['DISTANCE (px)', 	  '{:.2f}'.format(float(SEXTRACTOR_PHOTOMETRY['DISTANCE (px)'][0])), 	np.nan, np.nan, 'px'])
		catalog.add_row(['DISTANCE (arcsec)', '{:.2f}'.format(float(SEXTRACTOR_PHOTOMETRY['DISTANCE (arcsec)'][0])), np.nan, np.nan, 'arcsec'])

		# Add magnitudes

		# Add photutils keywords

		j					= 0

		for i in range(len(ZEROPOINT_SUMMARY)):
			if 'MAG_APER' in ZEROPOINT_SUMMARY['METHOD'][i]:
				if j 		== 0:
					catalog.add_row(['MAG_APER_PHOTUTILS',			np.nan, np.nan, np.nan, 'mag'])
					catalog.add_row(['MAG_APER_PHOTUTILS_3SIGMA',	np.nan, np.nan, np.nan, 'mag'])
					catalog.add_row(['FNU_APER_PHOTUTILS',			np.nan, np.nan, np.nan, 'mag'])
				else:
					catalog.add_row(['MAG_APER_' + str(j) + '_PHOTUTILS',		np.nan, np.nan, np.nan, 'mag'])
					catalog.add_row(['MAG_APER_' + str(j) + '_PHOTUTILS_3SIGMA',np.nan, np.nan, np.nan, 'mag'])
					catalog.add_row(['FNU_APER_' + str(j) + '_PHOTUTILS',		np.nan, np.nan, np.nan, 'mag'])

				j 			+= 1

		# Add information from Sextractor

		for key in [x for x in SEXTRACTOR_PHOTOMETRY.keys() if 'MAG_' in x]:

			mag				= '{:.3f}'.format(SEXTRACTOR_PHOTOMETRY[key][0])
			mag_errp		= '{:.3f}'.format(SEXTRACTOR_PHOTOMETRY[key.replace('MAG', 'MAGERRP')][0])
			mag_errm		= '{:.3f}'.format(SEXTRACTOR_PHOTOMETRY[key.replace('MAG', 'MAGERRM')][0])

			catalog.add_row([key, mag, mag_errp, mag_errm, 'mag'])

	else:

		msg					= 'No object found within {offset:.1f} arcsec from the source position'.format(offset=OFFSET)
		print(bcolors.WARNING + '\n{}\n'.format(msg) + bcolors.ENDC)
		LOGGER.info(msg)

		for key in ['XWIN_IMAGE', 'YWIN_IMAGE']:
			catalog.add_row([key+'_OBS',		np.nan, np.nan, np.nan, 'px'])

		catalog.add_row(['DISTANCE (px)',		np.nan, np.nan, np.nan, 'px'])
		catalog.add_row(['DISTANCE (arcsec)',	np.nan, np.nan, np.nan, 'arcsec'])

		# for key in ['X_IMAGE', 'Y_IMAGE']:
		# 	catalog.add_row([key+'_OBS',		np.nan, np.nan, np.nan, 'px'])

		# for key in ['DISTANCE (px)', 'DISTANCE (arcsec)']:
		# 	catalog.add_row([key,				np.nan, np.nan, np.nan, '...'])

		# Iterate over all apertures

		for i in range(len([key for key in PHOTUTILS_PHOTOMETRY.keys() if 'MAG_' in key])):

			# Remember: flux density is already in micro-Jansky. Only add 23.9

			if i 			== 0:

				mag			= '{:.3f}'.format(PHOTUTILS_PHOTOMETRY['MAG_APER_' 		+ str(i)][0])
				mag_errp	= '{:.3f}'.format(PHOTUTILS_PHOTOMETRY['MAGERRP_APER_'	+ str(i)][0])
				mag_errm	= '{:.3f}'.format(PHOTUTILS_PHOTOMETRY['MAGERRM_APER_'	+str(i)][0])
				mag_3sigma	= '{:.3f}'.format(23.9 - 2.5*np.log10(3*PHOTUTILS_PHOTOMETRY['FNUERR_APER_'+str(i)][0]))
				flux		= '{:.3e}'.format(PHOTUTILS_PHOTOMETRY['FNU_APER_' 		+ str(i)][0])
				flux_err	= '{:.3e}'.format(PHOTUTILS_PHOTOMETRY['FNUERR_APER_'	+ str(i)][0])

				catalog.add_row(['MAG_APER_PHOTUTILS', 			mag,		mag_errp,	mag_errm, 'mag'])
				catalog.add_row(['MAG_APER_PHOTUTILS_3SIGMA',	mag_3sigma,	np.nan,		np.nan,   'mag'])
				catalog.add_row(['FNU_APER_PHOTUTILS', 			flux,		flux_err,	flux_err, 'microJy'])

			else:

				mag			= '{:.3f}'.format(PHOTUTILS_PHOTOMETRY['MAG_APER_'		+str(i)][0])
				mag_errp	= '{:.3f}'.format(PHOTUTILS_PHOTOMETRY['MAGERRP_APER_'	+str(i)][0])
				mag_errm	= '{:.3f}'.format(PHOTUTILS_PHOTOMETRY['MAGERRM_APER_'	+str(i)][0])
				mag_3sigma	= '{:.3f}'.format(23.9 - 2.5*np.log10(3*PHOTUTILS_PHOTOMETRY['FNUERR_APER_'+str(i)][0]))
				flux		= '{:.3e}'.format(PHOTUTILS_PHOTOMETRY['FNU_APER_'		+str(i)][0])
				flux_err	= '{:.3e}'.format(PHOTUTILS_PHOTOMETRY['FNUERR_APER_'	+str(i)][0])

				catalog.add_row(['MAG_APER_PHOTUTILS_'+str(i), 			mag,		mag_errp,	mag_errm, 'mag'])
				catalog.add_row(['MAG_APER_PHOTUTILS_3SIGMA_'+str(i),	mag_3sigma,	np.nan,		np.nan,   'mag'])
				catalog.add_row(['FNU_APER_PHOTUTILS_'+str(i), 			flux,		flux_err,	flux_err, 'microJy'])

			i 				+= 1

		# Add Sextractor keywords

		for key in [x for x in SEXTRACTOR_PHOTOMETRY.keys() if 'MAG_' in x]:
			catalog.add_row([key, np.nan, np.nan, np.nan, 'mag'])

	return catalog

def setup_sextractor():
	default_conv	= ("""CONV NORM
# 3x3 ``all-ground'' convolution mask with FWHM = 2 pixels.
1 2 1
2 4 2
1 2 1
	""")

	default_nnw		= ("""NNW
# Neural Network Weights for the SExtractor star/galaxy classifier (V1.3)
# inputs:       9 for profile parameters + 1 for seeing.
# outputs:      ``Stellarity index'' (0.0 to 1.0)
# Seeing FWHM range: from 0.025 to 5.5'' (images must have 1.5 < FWHM < 5 pixels)
# Optimized for Moffat profiles with 2<= beta <= 4.

 3 10 10  1

-1.56604e+00 -2.48265e+00 -1.44564e+00 -1.24675e+00 -9.44913e-01 -5.22453e-01  4.61342e-02  8.31957e-01  2.15505e+00  2.64769e-01
 3.03477e+00  2.69561e+00  3.16188e+00  3.34497e+00  3.51885e+00  3.65570e+00  3.74856e+00  3.84541e+00  4.22811e+00  3.27734e+00

-3.22480e-01 -2.12804e+00  6.50750e-01 -1.11242e+00 -1.40683e+00 -1.55944e+00 -1.84558e+00 -1.18946e-01  5.52395e-01 -4.36564e-01 -5.30052e+00
 4.62594e-01 -3.29127e+00  1.10950e+00 -6.01857e-01  1.29492e-01  1.42290e+00  2.90741e+00  2.44058e+00 -9.19118e-01  8.42851e-01 -4.69824e+00
-2.57424e+00  8.96469e-01  8.34775e-01  2.18845e+00  2.46526e+00  8.60878e-02 -6.88080e-01 -1.33623e-02  9.30403e-02  1.64942e+00 -1.01231e+00
 4.81041e+00  1.53747e+00 -1.12216e+00 -3.16008e+00 -1.67404e+00 -1.75767e+00 -1.29310e+00  5.59549e-01  8.08468e-01 -1.01592e-02 -7.54052e+00
 1.01933e+01 -2.09484e+01 -1.07426e+00  9.87912e-01  6.05210e-01 -6.04535e-02 -5.87826e-01 -7.94117e-01 -4.89190e-01 -8.12710e-02 -2.07067e+01
-5.31793e+00  7.94240e+00 -4.64165e+00 -4.37436e+00 -1.55417e+00  7.54368e-01  1.09608e+00  1.45967e+00  1.62946e+00 -1.01301e+00  1.13514e-01
 2.20336e-01  1.70056e+00 -5.20105e-01 -4.28330e-01  1.57258e-03 -3.36502e-01 -8.18568e-02 -7.16163e+00  8.23195e+00 -1.71561e-02 -1.13749e+01
 3.75075e+00  7.25399e+00 -1.75325e+00 -2.68814e+00 -3.71128e+00 -4.62933e+00 -2.13747e+00 -1.89186e-01  1.29122e+00 -7.49380e-01  6.71712e-01
-8.41923e-01  4.64997e+00  5.65808e-01 -3.08277e-01 -1.01687e+00  1.73127e-01 -8.92130e-01  1.89044e+00 -2.75543e-01 -7.72828e-01  5.36745e-01
-3.65598e+00  7.56997e+00 -3.76373e+00 -1.74542e+00 -1.37540e-01 -5.55400e-01 -1.59195e-01  1.27910e-01  1.91906e+00  1.42119e+00 -4.35502e+00

-1.70059e+00 -3.65695e+00  1.22367e+00 -5.74367e-01 -3.29571e+00  2.46316e+00  5.22353e+00  2.42038e+00  1.22919e+00 -9.22250e-01 -2.32028e+00


 0.00000e+00
 1.00000e+00
	""")

	default_param	= ("""NUMBER                   # Running object number
XWIN_IMAGE               # Windowed position estimate along x                        [pixel]
YWIN_IMAGE               # Windowed position estimate along y                        [pixel]
ERRX2WIN_IMAGE           # Variance of position along x                              [pixel**2]
ERRY2WIN_IMAGE           # Variance of position along y                              [pixel**2]
ERRXYWIN_IMAGE           # Covariance of position between x and y                    [pixel**2]
X2WIN_IMAGE              # Windowed variance along x                                 [pixel**2]
Y2WIN_IMAGE              # Windowed variance along y                                 [pixel**2]
XYWIN_IMAGE              # Windowed covariance between x and y                       [pixel**2]
ELONGATION               # A_IMAGE/B_IMAGE
ALPHAWIN_J2000           # Windowed right ascension (J2000)                          [deg]
DELTAWIN_J2000           # windowed declination (J2000)                              [deg]
FLAGS                    # Extraction flags
FWHM_IMAGE               # FWHM assuming a gaussian core                             [pixel]
CLASS_STAR               # S/G classifier output
FLUX_APER(1)             # Flux vector within fixed circular aperture(s)             [count]
FLUXERR_APER(1)          # RMS error vector for aperture flux(es)                    [count]
BACKGROUND               # Background at centroid position                           [count]
FLUX_MAX                 # Peak flux above background                                [count]
FLUX_AUTO                # Flux within a Kron-like elliptical aperture               [count]
FLUXERR_AUTO             # RMS error for AUTO flux                                   [count]
KRON_RADIUS              # Kron apertures in units of A or B
FLUX_ISO                 # Isophotal flux                                            [count]
FLUXERR_ISO              # RMS error for isophotal flux                              [count]
ISOAREA_IMAGE            # Isophotal area above Analysis threshold                   [pixel**2]
MU_MAX                   # Peak surface brightness above background                  [mag * arcsec**(-2)]
FLUX_RADIUS              # Fraction-of-light radii                                   [pixel]
FLUX_PETRO               # Flux within a Petrosian-like elliptical aperture          [count]
FLUXERR_PETRO            # RMS error for PETROsian flux                              [count]
PETRO_RADIUS             # Petrosian apertures in units of A or B
SNR_WIN                  # Signal-to-noise ratio in a Gaussian window
	""")

	default_sex	= (
"""# Default configuration file for SExtractor 2.12.4
# EB 2010-10-10
#

#-------------------------------- Catalog ------------------------------------

CATALOG_NAME     test.cat       # name of the output catalog
CATALOG_TYPE     FITS_LDAC     # NONE,ASCII,ASCII_HEAD, ASCII_SKYCAT,
                                # ASCII_VOTABLE, FITS_1.0 or FITS_LDAC
PARAMETERS_NAME  default.param  # name of the file containing catalog contents

#------------------------------- Extraction ----------------------------------

DETECT_TYPE      CCD            # CCD (linear) or PHOTO (with gamma correction)
DETECT_MINAREA   3              # min. # of pixels above threshold

DETECT_THRESH    1.5            # <sigmas> or <threshold>,<ZP> in mag.arcsec-2
ANALYSIS_THRESH  1.5            # <sigmas> or <threshold>,<ZP> in mag.arcsec-2

FILTER           Y              # apply filter for detection (Y or N)?
FILTER_NAME      default.conv   # name of the file containing the filter

DEBLEND_NTHRESH  32             # Number of deblending sub-thresholds
DEBLEND_MINCONT  0.005          # Minimum contrast parameter for deblending

CLEAN            Y              # Clean spurious detections? (Y or N)?
CLEAN_PARAM      1.0            # Cleaning efficiency

#-------------------------------- WEIGHTing ----------------------------------

WEIGHT_TYPE      NONE           # type of WEIGHTing: NONE, BACKGROUND,
                                # MAP_RMS, MAP_VAR or MAP_WEIGHT
WEIGHT_IMAGE     weight.fits    # weight-map filename

#-------------------------------- FLAGging -----------------------------------

FLAG_IMAGE       flag.fits      # filename for an input FLAG-image
FLAG_TYPE        OR             # flag pixel combination: OR, AND, MIN, MAX
                                # or MOST

#------------------------------ Photometry -----------------------------------

PHOT_APERTURES   5              # MAG_APER aperture diameter(s) in pixels
PHOT_AUTOPARAMS  2.5, 3.5       # MAG_AUTO parameters: <Kron_fact>,<min_radius>
PHOT_PETROPARAMS 2.0, 3.5       # MAG_PETRO parameters: <Petrosian_fact>,
                                # <min_radius>
PHOT_AUTOAPERS   0.0,0.0        # <estimation>,<measurement> minimum apertures
                                # for MAG_AUTO and MAG_PETRO

SATUR_LEVEL      50000.0        # level (in ADUs) at which arises saturation
SATUR_KEY        SATURATE       # keyword for saturation level (in ADUs)

MAG_ZEROPOINT    0.0            # magnitude zero-point
MAG_GAMMA        4.0            # gamma of emulsion (for photographic scans)
GAIN             0.0            # detector gain in e-/ADU
GAIN_KEY         GAIN           # keyword for detector gain in e-/ADU
PIXEL_SCALE      1.0            # size of pixel in arcsec (0=use FITS WCS info)

#------------------------- Star/Galaxy Separation ----------------------------

SEEING_FWHM      1.2            # stellar FWHM in arcsec
STARNNW_NAME     default.nnw    # Neural-Network_Weight table filename

#------------------------------ Background -----------------------------------

BACK_TYPE        AUTO           # AUTO or MANUAL
BACK_VALUE       0.0            # Default background value in MANUAL mode
BACK_SIZE        64             # Background mesh: <size> or <width>,<height>
BACK_FILTERSIZE  3              # Background filter: <size> or <width>,<height>

#------------------------------ Check Image ----------------------------------

CHECKIMAGE_TYPE  NONE           # can be NONE, BACKGROUND, BACKGROUND_RMS,
                                # MINIBACKGROUND, MINIBACK_RMS, -BACKGROUND,
                                # FILTERED, OBJECTS, -OBJECTS, SEGMENTATION,
                                # or APERTURES
CHECKIMAGE_NAME  check.fits     # Filename for the check-image

#--------------------- Memory (change with caution!) -------------------------

MEMORY_OBJSTACK  3000           # number of objects in stack
MEMORY_PIXSTACK  300000         # number of pixels in stack
MEMORY_BUFSIZE   1024           # number of lines in buffer

#------------------------------- ASSOCiation ---------------------------------

ASSOC_NAME       sky.list       # name of the ASCII file to ASSOCiate
ASSOC_DATA       2,3,4          # columns of the data to replicate (0=all)
ASSOC_PARAMS     2,3,4          # columns of xpos,ypos[,mag]
ASSOC_RADIUS     2.0            # cross-matching radius (pixels)
ASSOC_TYPE       NEAREST        # ASSOCiation method: FIRST, NEAREST, MEAN,
                                # MAG_MEAN, SUM, MAG_SUM, MIN or MAX
ASSOCSELEC_TYPE  MATCHED        # ASSOC selection type: ALL, MATCHED or -MATCHED

#----------------------------- Miscellaneous ---------------------------------

VERBOSE_TYPE     NORMAL         # can be QUIET, NORMAL or FULL
HEADER_SUFFIX    .head          # Filename extension for additional headers
WRITE_XML        N              # Write XML file (Y/N)?
XML_NAME         sex.xml        # Filename for XML output
XSL_URL          file:///usr/local/share/sextractor/sextractor.xsl
                                # Filename for XSL style-sheet
	""")

	output 	= open('default.param', 'w')
	output.write(default_param)
	output.close()

	output 	= open('default.sex', 'w')
	output.write(default_sex)
	output.close()

	output 	= open('default.nnw', 'w')
	output.write(default_nnw)
	output.close()

	output 	= open('default.conv', 'w')
	output.write(default_conv)
	output.close()

	return None

def sextractor_photometry(
					ANALYSIS_THRESH	= 1,
					ASSOC_NAME		= None,
					ASSOC_PARAMS	= "1,2",
					ASSOC_RADIUS	= 10,
					BACK_SIZE		= 64,
					BACK_FILTERSIZE	= 3,
#					CORRELATED		= False,
					DEBLEND_NTHRESH	= 64,
					DEBLEND_MINCONT	= 0.00001,
					DETECT_THRESH	= 1,
					FITS			= "",
					FLAG			= "",
					GAIN			= 1,
					LOGGER			= None,
					LOGLEVEL		= 'WARNING',
					PARAMS 			=  "DEFAULT",
					PATH			= None,
					PHOT_APERTURES	= None, 
					REF_FILE		= ""):

	sew				= sewpy.SEW(loglevel=LOGLEVEL,
								config={"ANALYSIS_THRESH": 	ANALYSIS_THRESH,
										"ASSOC_PARAMS": 	ASSOC_PARAMS,
										"ASSOC_RADIUS": 	ASSOC_RADIUS,
										"BACK_SIZE":		BACK_SIZE,
										"BACK_FILTERSIZE":	BACK_FILTERSIZE,
										'CHECKIMAGE_NAME': 	"check_"+FITS,
										'CHECKIMAGE_TYPE': 	"APERTURES",
										"DEBLEND_NTHRESH": 	DEBLEND_NTHRESH,
										"DEBLEND_MINCONT": 	DEBLEND_MINCONT,
										"DETECT_MINAREA": 	5,
										"DETECT_THRESH": 	DETECT_THRESH,
										"GAIN_KEY": 		get_gain(FITS, GAIN, LOGGER),
										"PHOT_APERTURES": 	"",
										"PHOT_FLUXFRAC":	0.5,
										"PHOT_AUTOPARAMS": 	"2.5, 3.5",
										"PHOT_PETROPARAMS": "2.0, 3.5",
										"SATURATION":		100000000,
										}
					)

	if PARAMS		== "DEFAULT":
		params_out	= ["XWIN_IMAGE", "YWIN_IMAGE", "ALPHAWIN_J2000", "DELTAWIN_J2000",
						"MAG_AUTO", "MAGERR_AUTO",
						"MAG_PETRO", "MAGERR_PETRO",
						"FLUX_AUTO", "FLUXERR_AUTO",
						"FLUX_PETRO", "FLUXERR_PETRO",
						"FWHM_IMAGE", "FWHM_WORLD",
						"A_IMAGE", "B_IMAGE", "THETA_IMAGE", "KRON_RADIUS",
						"FLUX_RADIUS", "FLAGS"]
	else:
		params_out	= PARAMS

	if ASSOC_NAME	!= None:
		params_out	+= ["VECTOR_ASSOC", "NUMBER_ASSOC"]

	if ASSOC_NAME	!= None:
		sew.config["ASSOC_NAME"] = ASSOC_NAME

	if isinstance(PHOT_APERTURES, np.ndarray):
		params_out	+= list(np.array([("MAG_APER(" + str(i+1) + ")", "MAGERR_APER(" + str(i+1) + ")") for i in range(len(PHOT_APERTURES))]).flatten())
		params_out	+= list(np.array([("FLUX_APER(" + str(i+1) + ")", "FLUXERR_APER(" + str(i+1) + ")") for i in range(len(PHOT_APERTURES))]).flatten())
		sew.config["PHOT_APERTURES"]	= ','.join([sew.config["PHOT_APERTURES"]+str(PHOT_APERTURES[i]) for i in range(len(PHOT_APERTURES))])

	else:
		params_out	+= ['MAG_APER', 'MAGERR_APER']
		params_out	+= ['FLUX_APER', 'FLUXERR_APER']
		sew.config["PHOT_APERTURES"]	= PHOT_APERTURES

	sew.params		= params_out

	if REF_FILE		== "":
		output		= sew(FITS)
	else:
		output		= sew(",".join([REF_FILE,FITS]))

	output_path		= os.path.dirname(output['logfilepath'])

	print('\nPath of the temporary files: %s\n' %output_path)

	for file in glob.glob(output_path+'/'+FITS[0]+'*cat*'):
		os.system(" mv %s %s" %(file, PATH + FITS.split(".fits")[0]+"_"+FLAG+".phot"))

	for file in glob.glob(output_path+'/'+FITS[0]+'*log*'):
		os.system(" mv %s %s" %(file, PATH + FITS.split(".fits")[0]+"_"+FLAG+".log"))

	os.system("rm -r %s" %output_path)

	return output['table']

def sextractor_postprocess(DATA, PRINTHELP=True):
	"""
	Reprocesses the Sextractor output. Sets negative fluxes to NaN and recalculates magnitude errors. Done to be consistent with forced photometry.
	"""

	if PRINTHELP:
		print(bcolors.WARNING + 'Recalculate magnitude errors [ mag_err_+/- = -2.5 log (F -/+ dF)/F ]' + bcolors.ENDC)
		print(bcolors.WARNING + 'Runtime warning is expected if F < 0. Objects with negative flux values' + bcolors.ENDC)
		print(bcolors.WARNING + 'are converted to 3 sigma limits [ mag = -2.5 log (3 dF); mag_err = -99 ]' + bcolors.ENDC)

	for key in [x for x in DATA.keys() if 'FLUX_' in x and 'RADIUS' not in x]:
		if any(DATA[key] < 0.):
			DATA[key][DATA[key] <= 0.] = np.nan
			DATA[key.replace('FLUX_', 'FLUXERR_')][DATA[key] <= 0] = np.nan

	for key in [x for x in DATA.keys() if 'FLUXERR_' in x]:
		errp		= -2.5 * np.log10(DATA[key.replace('FLUXERR_', 'FLUX_')] - DATA[key]) + 2.5 * np.log10(DATA[key.replace('FLUXERR_', 'FLUX_')])
		errm		= +2.5 * np.log10(DATA[key.replace('FLUXERR_', 'FLUX_')] + DATA[key]) - 2.5 * np.log10(DATA[key.replace('FLUXERR_', 'FLUX_')])
		mid_error	= abs(errp + errm) / 2.

		DATA[key.replace('FLUXERR_', 'MAGERR_')]		= mid_error
		DATA[key.replace('FLUXERR_', 'MAGERRP_')]		= errp
		DATA[key.replace('FLUXERR_', 'MAGERRM_')]		= errm

	return DATA

def zeropoint(TABLE_REF, TABLE_NEW, FITS='', LOGGER=None, NITER=30000, PATH='', TOLERANCE=1):

	print(bcolors.OKGREEN + 'Bootstrap ZP from ' + str(NITER) + ' resamplings\n' + bcolors.ENDC)

	for key in TABLE_REF.keys():
		if key not in ['RA', 'DEC', 'MAG_CAT', 'MAGERR_CAT']:
			del TABLE_REF[key]

	TABLE_REF_keys			= TABLE_REF.keys()

	TABLE_NEW_keys			= ['ALPHAWIN_J2000', 'DELTAWIN_J2000', 'XWIN_IMAGE', 'YWIN_IMAGE', 'MAG_AUTO', 'MAGERR_AUTO', 'MAG_PETRO',
							'MAGERR_PETRO', 'FLUX_AUTO', 'FLUXERR_AUTO', 'FLUX_PETRO', 'FLUXERR_PETRO', 'FWHM_IMAGE', 'FWHM_WORLD',
							'A_IMAGE', 'B_IMAGE', 'THETA_IMAGE', 'KRON_RADIUS', 'FLUX_RADIUS', 'FLAGS', 'VECTOR_ASSOC', 'NUMBER_ASSOC',
							'MAG_APER', 'MAG_APER_1', 'MAG_APER_2', 'MAG_APER_3', 'MAGERR_APER', 'MAGERR_APER_1', 'MAGERR_APER_2',
							'MAGERR_APER_3', 'FLUX_APER', 'FLUX_APER_1', 'FLUX_APER_2', 'FLUX_APER_3', 'FLUXERR_APER', 'FLUXERR_APER_1',
							'FLUXERR_APER_2', 'FLUXERR_APER_3']
	
	TABLE_REF				= np.asarray(TABLE_REF[TABLE_REF_keys]).view((float, len(TABLE_REF.dtype.names)))
	TABLE_NEW				= np.asarray(TABLE_NEW[TABLE_NEW_keys]).view((float, len(TABLE_NEW.dtype.names)))

	# pdb.set_trace()

	merged					= cat_tools.wrapper_crossmatch(TABLE_REF, TABLE_NEW, TOLERANCE)
	merged					= table.Table(merged, names=TABLE_NEW_keys + TABLE_REF_keys + ['DIST'])

	merged['DIST']			= merged['DIST']*3600
	merged['DIST'].format	='.3f'

	# If the ZP array is empty, the WCS system of the image is a bit off. Increase cross-match radius.
	# print(merged)

	# Extract magnitude keywords

	keys_mag				= [x for x in merged.keys() if ('MAG_' in x) and ('MAG_CAT' != x) and ('MAG_INS' != x)]

	# Setup table for zeropoint calculation

	result					= table.Table(names=('METHOD', 'ZP', 'ZP_ERRP', 'ZP_ERRM', 'NUMBER'), dtype=('S100', 'f', 'f', 'f', 'g'))
	result['NUMBER'].format = '7g'

	# Setup up ZP diagnostic plot

	fig						= plt.figure(5, figsize=(np.sqrt(2) * 9,9))
	fig.subplots_adjust(hspace=0.2, wspace=0.3)

	ax						= fig.add_subplot(111)
	ax.spines['top'].set_color('none')
	ax.spines['bottom'].set_color('none')
	ax.spines['left'].set_color('none')
	ax.spines['right'].set_color('none')
	ax.tick_params(labelcolor='w', top='off', bottom='off', left='off', right='off')

	i						= 0

	# Compute zeropoint

	for key in keys_mag:


		temp_zp				= merged['MAG_CAT'] - merged[key]
		temp_zp_err			= np.sqrt(merged['MAGERR_CAT']**2 + merged['MAGERR_'+key.split('_')[1]]**2)

		mask_negative		= np.where(temp_zp > 0)[0]

		temp_zp				= np.array([temp_zp[mask_negative], temp_zp_err[mask_negative]]).T

		# Print individual ZP measurements

		print(bcolors.OKGREEN + key + bcolors.ENDC)

		print(np.array(['{:.3f}'.format(j) for j in temp_zp[:,0]]))
		print(np.array(['{:.3f}'.format(j) for j in temp_zp[:,1]]))

		print('')

		LOGGER.info(key)
		LOGGER.info(np.array(['{:.3f}'.format(j) for j in temp_zp[:,0]]))
		LOGGER.info(np.array(['{:.3f}'.format(j) for j in temp_zp[:,1]]))


		if len(temp_zp) 	> 0:
			temp_zp_ana		= np.array(stat_tools.statNclip(temp_zp, NITER=NITER))
			result.add_row(np.hstack([key, temp_zp_ana]))

		else:
			result.add_row(np.hstack([key, np.zeros(4)]))

		# Add to plot

		zp_plot				= fig.add_subplot(len(keys_mag)/3 if len(keys_mag)%3 == 0 else len(keys_mag)/3 + 1, 3, i+1)

		if len(temp_zp) 	> 0:

			zp_50ile		= np.percentile(temp_zp[:,0], 50)
			zp_25ile		= np.percentile(temp_zp[:,0], 25)
			zp_75ile		= np.percentile(temp_zp[:,0], 75)

			mask_good		= np.where( ( temp_zp[:, 0] > (zp_25ile - 1.5*(zp_75ile-zp_25ile)) ) & ( temp_zp[:, 0] < (zp_75ile + 1.5*(zp_75ile-zp_25ile)) ))[0]
			mask_bad		= np.where( ( temp_zp[:, 0] < (zp_25ile - 1.5*(zp_75ile-zp_25ile)) ) | ( temp_zp[:, 0] > (zp_75ile + 1.5*(zp_75ile-zp_25ile)) ))[0]

			zp_plot.axhline(temp_zp_ana[0], lw=4, color=vigit_color_12)
			zp_plot.axhline(temp_zp_ana[0]+temp_zp_ana[1], lw=2, color=vigit_color_12, ls='--')
			zp_plot.axhline(temp_zp_ana[0]-temp_zp_ana[2], lw=2, color=vigit_color_12, ls='--')

			zp_plot.axhline(zp_75ile + 1.5*(zp_75ile-zp_25ile), lw=2, color=vigit_color_12, ls=':')
			zp_plot.axhline(zp_25ile - 1.5*(zp_75ile-zp_25ile), lw=2, color=vigit_color_12, ls=':')

			if len(mask_bad) > 0:
				zp_plot.errorbar(merged['MAG_CAT'][mask_negative][mask_bad],  temp_zp[:, 0][mask_bad],  temp_zp[:, 1][mask_bad],  marker='o', ms=9, color='0.75', elinewidth=2, capsize=0, lw=0)

			if len(mask_good) > 0:
				zp_plot.errorbar(merged['MAG_CAT'][mask_negative][mask_good], temp_zp[:, 0][mask_good], temp_zp[:, 1][mask_good], marker='o', ms=9, color='k', elinewidth=2, capsize=0, lw=0)

			zp_plot.set_xlim(min(merged['MAG_CAT'][mask_negative]) - 0.5, max(merged['MAG_CAT'][mask_negative]) + 0.5)

			majorLocator	= plt.MultipleLocator(1)
			zp_plot.xaxis.set_major_locator(majorLocator)

		zp_plot.grid(True)
		zp_plot.text(right - 0.05, top - 0.05, key.replace('_', '\_'), ha='right', va='top', transform = zp_plot.transAxes, fontsize=legend_size)

		i					+= 1

	ax.set_xlabel('Apparent magnitude', )
	ax.set_ylabel('Zeropoint')
	ax.yaxis.set_label_coords(-0.1, 0.5)

	plt.savefig(PATH+FITS.replace('.fits', '_zp.pdf'), dpi=600)

	# Diagnostic plots (cont'ed)

	# FWHM distribution

	plt.figure(6, figsize=(np.sqrt(2) * 9,9))
	fwhm_plot				= plt.subplot(111)

	if all(merged['FWHM_IMAGE'] == 0):

		fwhm_plot.hist(merged['FLUX_RADIUS']*2/1.1, range=(0,20), density=1, color=vigit_color_1)
		fwhm_plot.hist(merged['FLUX_RADIUS']*2/1.1, range=(0,20), density=1, color='k', bins=10000, cumulative=True, histtype='step', lw=4)
		fwhm_plot.axvline(np.percentile(merged['FLUX_RADIUS']*2/1.1, 50), lw=4, color='k')
		fwhm_plot.axvline(np.percentile(merged['FLUX_RADIUS']*2/1.1, 50-34), lw=2, ls=':', color='k')
		fwhm_plot.axvline(np.percentile(merged['FLUX_RADIUS']*2/1.1, 50+34), lw=2, ls=':', color='k')

	else:

		fwhm_plot.hist(merged['FWHM_IMAGE'], range=(0,30), density=1, color=vigit_color_1)
		fwhm_plot.hist(merged['FWHM_IMAGE'], range=(0,30), density=1, color='k', bins=10000, cumulative=True, histtype='step', lw=4)
		fwhm_plot.axvline(np.percentile(merged['FWHM_IMAGE'], 50), lw=4, color='k')
		fwhm_plot.axvline(np.percentile(merged['FWHM_IMAGE'], 50-34), lw=2, ls=':', color='k')
		fwhm_plot.axvline(np.percentile(merged['FWHM_IMAGE'], 50+34), lw=2, ls=':', color='k')

	fwhm_plot.set_xlim(0,30)
	fwhm_plot.set_ylim(0,1.1)
	fwhm_plot.set_xlabel('FWHM (px)')
	fwhm_plot.set_ylabel('Histogramme')

	plt.savefig(PATH+FITS.replace('.fits', '_fwhm.pdf'), dpi=600)

	return result