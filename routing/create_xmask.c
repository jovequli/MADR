#include <stdio.h>
#include <stdlib.h>
#include <strings.h>
#include <math.h>

double get_dist(double,double,double,double);

int main(int argc, char *argv[]) {
/***********************************************************************
  clac_xmask.c         Keith Cherkauer            November 24, 1998

  This program reads the routing model direction file, and uses the
  cell location and direction of flow to determine the length of the
  flow path through the grid cell.  It uses the routine new_get_dist.c
  to compute the actual horizontal, vertical, and diagonal distances
  in meters on the globe.

  1-8-99  Modified for newest version of routing model which 
          uses standard ARC/INFO ASCII Grid headers, and reads
          xmask values in meters.                             KAC

***********************************************************************/

  FILE *fdirec, *fxmask;

  int    row, col, nrows, ncols;
  int    direc;
  double cell_lat, cell_lng;
  double ll_lat, ll_lng;
  double cellsize;
  double NODATA;

  if(argc!=3) {
    fprintf(stderr,"Usage: %s <direc file> <xmask file>\n",argv[0]);
    exit(0);
  }

  if((fdirec=fopen(argv[1],"r"))==NULL) {
    fprintf(stderr,"ERROR: Unable to open input file %s\n",argv[1]);
    exit(0);
  }

  if((fxmask=fopen(argv[2],"w"))==NULL) {
    fprintf(stderr,"ERROR: Unable to open output file %s\n",argv[2]);
    exit(0);
  }

  /** Read Header **/
  fscanf(fdirec,"%*s %i", &ncols);
  fscanf(fdirec,"%*s %i", &nrows);
  fscanf(fdirec,"%*s %lf",&ll_lng);
  fscanf(fdirec,"%*s %lf",&ll_lat);
  fscanf(fdirec,"%*s %lf",&cellsize);
  fscanf(fdirec,"%*s %lf",&NODATA);
  fprintf(fxmask,"ncols\t%i\n", ncols);
  fprintf(fxmask,"nrows\t%i\n", nrows);
  fprintf(fxmask,"xllcorner\t%.1lf\n",ll_lng);
  fprintf(fxmask,"yllcorner\t%.1lf\n",ll_lat);
  fprintf(fxmask,"cellsize\t%.3lf\n",cellsize);
  fprintf(fxmask,"NODATA_value\t%.0lf\n",NODATA);

  /** Process Direction Number **/
  for(row=0;row<nrows;row++) {
    for(col=0;col<ncols;col++) {
      cell_lat = ((double)(nrows-row)-0.5)*cellsize+ll_lat;
      cell_lng = ((double)col+0.5)*cellsize+ll_lng;

      fscanf(fdirec,"%i",&direc);

      if(direc!=NODATA) {
	if(direc==1 || direc==5) {
	  fprintf(fxmask,"%.0lf",get_dist(cell_lat-0.5*cellsize,cell_lng,
					cell_lat+0.5*cellsize,cell_lng)*1000.);
	}
	else if(direc==3 || direc==7) {
	  fprintf(fxmask,"%.0lf",get_dist(cell_lat,cell_lng-0.5*cellsize,
					cell_lat,cell_lng+0.5*cellsize)*1000.);
	}
	else {
	  fprintf(fxmask,"%.0lf",get_dist(cell_lat-0.5*cellsize,
					cell_lng-0.5*cellsize,
					cell_lat+0.5*cellsize,
					cell_lng+0.5*cellsize)*1000.);
	}
      }
      else {
	fprintf(fxmask,"%.0lf",NODATA);
      }
      if(col!=ncols-1) fprintf(fxmask,"\t");
    }
    fprintf(fxmask,"\n");
  }

  fclose(fdirec);
  fclose(fxmask);

  return(0);
}

/***************************************************************************
  Function: double distance(double lat1, double long1, double lat2, double long2)
  Returns : distance between two locations ¼ÆËãÇòÃæ¾àÀë
****************************************************************************/

#ifndef _E_RADIUS
#define E_RADIUS 6371.0         /* average radius of the earth */
#endif

#ifndef _PI
#define PI 3.1415
#endif

double get_dist(double lat1, double long1, double lat2, double long2)
{
  double theta1;
  double phi1;
  double theta2;
  double phi2;
  double dtor;
  double term1;
  double term2;
  double term3;
  double temp;
  double dist;

  dtor = 2.0*PI/360.0;
  theta1 = dtor*long1;
  phi1 = dtor*lat1;
  theta2 = dtor*long2;
  phi2 = dtor*lat2;
  term1 = cos(phi1)*cos(theta1)*cos(phi2)*cos(theta2);
  term2 = cos(phi1)*sin(theta1)*cos(phi2)*sin(theta2);
  term3 = sin(phi1)*sin(phi2);
  temp = term1+term2+term3;
  temp = (double) (1.0 < temp) ? 1.0 : temp;
  dist = E_RADIUS*acos(temp);

  return dist;
}  

#undef E_RADIUS
#undef PI
